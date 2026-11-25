package tools

import (
	"strings"
	"testing"
)

func TestMaskHidesSecretsAndKeepsTheRest(t *testing.T) {
	for name, c := range map[string]struct {
		in   string
		gone []string // must not survive
		keep []string // must survive
	}{
		"env file": {
			in:   "POSTGRES_USER=app\nPOSTGRES_PASSWORD=hunter2hunter2\nTZ=Europe/Berlin\nJWT_SECRET=abc.def\n",
			gone: []string{"hunter2hunter2", "abc.def"},
			keep: []string{"POSTGRES_USER=app", "TZ=Europe/Berlin", "POSTGRES_PASSWORD="},
		},
		"compose environment list": {
			in:   "    environment:\n      - DB_PASSWORD=s3cr3t-value\n      - LOG_LEVEL=debug\n      API_TOKEN: \"tok-123456\"\n",
			gone: []string{"s3cr3t-value", "tok-123456"},
			keep: []string{"LOG_LEVEL=debug", "environment:", `API_TOKEN: "«masked:secret_key»"`},
		},
		"references are not secrets": {
			in:   "      DB_PASSWORD: ${DB_PASSWORD}\n      TOKEN: $TOKEN\n",
			keep: []string{"${DB_PASSWORD}", "$TOKEN"},
		},
		"author is not auth": {
			in:   "AUTHOR=Jane\nOAUTH_TOKEN=abc123456\n",
			gone: []string{"abc123456"},
			keep: []string{"AUTHOR=Jane"},
		},
		"json log line": {
			in:   `{"level":"info","msg":"login","password":"pw-for-jane","user":"jane"}`,
			gone: []string{"pw-for-jane"},
			keep: []string{`"user":"jane"`, `"msg":"login"`},
		},
		"url credentials": {
			in:   "connecting to postgres://app:pa55w0rd@db:5432/app ...",
			gone: []string{"pa55w0rd"},
			keep: []string{"postgres://app:", "@db:5432/app"},
		},
		"bearer and jwt": {
			in:   "Authorization: Bearer abcdefghijklmnopqrstuvwx\nid_token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIzNDU2",
			gone: []string{"abcdefghijklmnopqrstuvwx", "eyJhbGciOiJIUzI1NiJ9", "c2lnbmF0dXJlMTIzNDU2"},
		},
		"connection string and command line": {
			in:   "Server=db;User Id=sa;Password=Sup3rSecret;\nexec mysqld --password=topsecret123 --port=3306",
			gone: []string{"Sup3rSecret", "topsecret123"},
			keep: []string{"Server=db;", "--port=3306"},
		},
		"private key": {
			in:   "before\n-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\nAAAAB\n-----END OPENSSH PRIVATE KEY-----\nafter",
			gone: []string{"b3BlbnNzaC1rZXktdjEAAAAA", "AAAAB"},
			keep: []string{"before", "after"},
		},
		"provider keys": {
			in:   "ghp_abcdefghijklmnopqrstuvwxyz0123 and AKIAIOSFODNN7EXAMPLE and sk-abcdefghijklmnopqrstuv",
			gone: []string{"ghp_abcdefghijklmnopqrstuvwxyz0123", "AKIAIOSFODNN7EXAMPLE", "sk-abcdefghijklmnopqrstuv"},
		},
		"container ids and digests stay": {
			in:   "container 3f4a5b6c7d8e9f0a1b2c3d4e5f60718293a4b5c6d7e8f9012345678901234567 image sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
			keep: []string{"3f4a5b6c7d8e9f0a1b2c3d4e5f60718293a4b5c6d7e8f9012345678901234567", "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},
		},
	} {
		t.Run(name, func(t *testing.T) {
			out, n := Mask(c.in)
			for _, g := range c.gone {
				if strings.Contains(out, g) {
					t.Errorf("%q survived in %q", g, out)
				}
			}
			for _, k := range c.keep {
				if !strings.Contains(out, k) {
					t.Errorf("%q was lost in %q", k, out)
				}
			}
			if len(c.gone) > 0 && n == 0 {
				t.Errorf("nothing counted as masked")
			}
			if len(c.gone) == 0 && n != 0 {
				t.Errorf("%d masks where nothing is secret: %q", n, out)
			}
		})
	}
}

func TestMaskIsIdempotentAndNamesTheRule(t *testing.T) {
	once, n := Mask("DB_PASSWORD=abcdef123456")
	twice, n2 := Mask(once)
	if once != twice || n != 1 || n2 != 0 {
		t.Fatalf("%q %d / %q %d", once, n, twice, n2)
	}
	if !strings.Contains(once, "«masked:secret_key»") {
		t.Fatal(once)
	}
}

// The line from the real-hardware test (T9): only the first secret of the line used to go.
func TestMaskInlineKeysAnywhereInTheLine(t *testing.T) {
	in := "login password=letmein123 api_key=not-a-real-key-123456 token=ghp_abcdefghijklmnopqrstuvwxyz0123"
	out, n := Mask(in)
	for _, g := range []string{"letmein123", "not-a-real-key-123456", "ghp_abcdefghijklmnopqrstuvwxyz0123"} {
		if strings.Contains(out, g) {
			t.Errorf("%q survived in %q", g, out)
		}
	}
	if !strings.HasPrefix(out, "login password=") || !strings.Contains(out, " api_key=«masked:") || !strings.Contains(out, " token=«masked:") || n != 3 {
		t.Fatalf("%q (%d)", out, n)
	}
}

func TestMaskKeysWhateverTheValueLooksLike(t *testing.T) {
	for name, c := range map[string]struct {
		in   string
		gone []string
		keep []string
	}{
		"dotenv of a vpn container": {
			in: "WIREGUARD_PRIVATE_KEY=wOEI9rqqbDwnN8/Bpp22sVz48T71vJ4fYmFWujulwUU=\nOPENVPN_USER=alice\nOPENVPN_PASSWORD=p@ss w0rd with spaces\n" +
				"SERVER_COUNTRIES=Netherlands\nFIREWALL_OUTBOUND_SUBNETS=10.0.0.0/24\n",
			gone: []string{"wOEI9rqqbDwnN8", "p@ss w0rd", "with spaces"},
			keep: []string{"OPENVPN_USER=alice", "SERVER_COUNTRIES=Netherlands", "FIREWALL_OUTBOUND_SUBNETS=10.0.0.0/24", "WIREGUARD_PRIVATE_KEY="},
		},
		"dotenv api keys and urls": {
			in: "OPENAI_API_KEY=abc\nSONARR_API_KEY=0123456789abcdef\nSTRIPE_APIKEY=x1\nDATABASE_URL=postgres://app:s3cret/with/slash@db:5432/app\n" +
				"REDIS_URL=redis://:redispw@cache:6379/0\nBROKER=amqp://guest:guest@mq/\nTZ=Europe/Berlin\n",
			gone: []string{"abc", "0123456789abcdef", "x1", "s3cret", "redispw"},
			keep: []string{"TZ=Europe/Berlin", "@db:5432/app", "@cache:6379/0", "OPENAI_API_KEY="},
		},
		"short and odd values": {
			in:   "password=1\nsecret: true\nDB_PASS=no\nclient_secret=0\n",
			gone: []string{"=1", ": true", "=no", "=0"},
		},
		"json with neighbors": {
			in:   "{\n  \"user\": \"jane\",\n  \"password\": \"pw1\",\n  \"apiKey\": \"k-2\",\n  \"n_tokens\": 512\n}\n",
			gone: []string{"pw1", "k-2"},
			keep: []string{`"user": "jane"`, `"password": "«masked:secret_key»",`, `"n_tokens": 512`},
		},
		"json inside a log line": {
			in:   `level=info msg="{\"client_secret\":\"abc123\",\"user\":\"jane\"}" pw=zzz`,
			gone: []string{"abc123", "zzz"},
			keep: []string{`\"user\":\"jane\"`, "level=info"},
		},
		"yaml": {
			in:   "auth:\n  username: jane\n  password: 'my pass'\n  credential: abc def\nprivate_key: |\n  not a pem\n",
			gone: []string{"my pass", "abc def"},
			keep: []string{"username: jane", "auth:"},
		},
		"authorization headers": {
			in:   "Authorization: Bearer short\nproxy-authorization: Basic dXNlcjpwdw==\n> GET / -H \"Authorization: Bearer tok123\"\nAuthorization: secret-without-scheme",
			gone: []string{"short", "dXNlcjpwdw", "tok123", "secret-without-scheme"},
			keep: []string{"Authorization: Bearer ", "proxy-authorization: Basic ", "-H \"Authorization: Bearer "},
		},
		"url query and userinfo": {
			in:   "GET /hook?user=jane&token=abc123&x=1 from ftp://bob:hunter2@files/x and https://h/?password=pw&a=b",
			gone: []string{"abc123", "hunter2", "=pw&"},
			keep: []string{"user=jane", "&x=1", "&a=b", "ftp://bob:"},
		},
		"connection string": {
			in:   "Server=db;User Id=sa;Password=Sup3rSecret;Encrypt=true",
			gone: []string{"Sup3rSecret"},
			keep: []string{"Server=db;", "Encrypt=true"},
		},
		"names that are no secrets": {
			in:   "passed=3 failed=0 author=jane n_tokens = 512 prompt_tokens=10 compass=north keyboard=us\nmsg: author: Jane\n",
			keep: []string{"passed=3", "author=jane", "n_tokens = 512", "prompt_tokens=10", "compass=north", "keyboard=us", "author: Jane"},
		},
	} {
		t.Run(name, func(t *testing.T) {
			out, n := Mask(c.in)
			for _, g := range c.gone {
				if strings.Contains(out, g) {
					t.Errorf("%q survived in %q", g, out)
				}
			}
			for _, k := range c.keep {
				if !strings.Contains(out, k) {
					t.Errorf("%q was lost in %q", k, out)
				}
			}
			if len(c.gone) > 0 && n == 0 {
				t.Errorf("nothing counted as masked")
			}
			if len(c.gone) == 0 && n != 0 {
				t.Errorf("%d masks where nothing is secret: %q", n, out)
			}
			again, n2 := Mask(out)
			if again != out || n2 != 0 {
				t.Errorf("not idempotent: %q -> %q (%d)", out, again, n2)
			}
		})
	}
}
