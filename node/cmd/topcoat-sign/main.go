// Command topcoat-sign signs and publishes topcoat-node builds for the update channel (E2-06, D50).
// It runs on the release machine, never on a node or the hub; the private key never leaves it.
//
//	topcoat-sign keygen  --out FILE
//	topcoat-sign sign    --key FILE --binary FILE --version V --os OS --arch ARCH [--channel stable] [--out-dir DIR]
//	topcoat-sign upload  --hub URL --dir DIR [--ca-fingerprint FP | --ca-file FILE | --insecure-http]
//	topcoat-sign publish --key FILE --binary FILE --version V --os OS --arch ARCH --hub URL [...]
//	topcoat-sign verify  --pub KEY --dir DIR
//
// upload and publish read the hub admin token from $TOPCOAT_ADMIN_TOKEN (never from a flag: flags
// end up in shell history and process lists).
package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"topcoat/node/internal/hubtls"
	"topcoat/node/internal/update"
)

const (
	tokenEnv  = "TOPCOAT_ADMIN_TOKEN" //nolint:gosec // the name of the variable, not a secret
	keyHeader = "topcoat-update-key-v1"
)

const usage = `topcoat-sign - sign and publish topcoat-node builds

  topcoat-sign keygen --out FILE
      create an ed25519 signing key (FILE is private, FILE.pub holds the public key to build into nodes)
  topcoat-sign sign --key FILE --binary FILE --version V --os OS --arch ARCH [--channel stable] [--out-dir DIR]
      write manifest.json and manifest.sig for a build
  topcoat-sign upload --hub URL [--dir DIR] [--ca-fingerprint FP | --ca-file FILE | --insecure-http]
      publish a signed build to the hub (admin token in $` + tokenEnv + `)
  topcoat-sign publish ...   sign and upload in one step (flags of sign and upload)
  topcoat-sign verify --pub KEY [--dir DIR]
      check a signed build against a public key
`

func main() {
	os.Exit(run(context.Background(), os.Args[1:], os.Stdout, os.Stderr))
}

func run(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprint(stderr, usage)
		return 2
	}
	var err error
	switch args[0] {
	case "keygen":
		err = cmdKeygen(args[1:], stdout)
	case "sign":
		_, err = cmdSign(args[1:], stdout)
	case "upload":
		err = cmdUpload(ctx, args[1:], stdout)
	case "publish":
		err = cmdPublish(ctx, args[1:], stdout)
	case "verify":
		err = cmdVerify(args[1:], stdout)
	case "help", "-h", "--help":
		fmt.Fprint(stdout, usage)
		return 0
	default:
		fmt.Fprintf(stderr, "unknown command %q\n\n%s", args[0], usage)
		return 2
	}
	if errors.Is(err, flag.ErrHelp) {
		return 0
	}
	if err != nil {
		fmt.Fprintln(stderr, "topcoat-sign:", err)
		return 1
	}
	return 0
}

func newFlags(name string) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	return fs
}

// --- keys ---------------------------------------------------------------------

func cmdKeygen(args []string, stdout io.Writer) error {
	fs := newFlags("keygen")
	out := fs.String("out", "", "file for the private key (the public key goes to <file>.pub)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *out == "" {
		return errors.New("keygen: --out is required")
	}
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return err
	}
	body := keyHeader + " " + base64.StdEncoding.EncodeToString(priv.Seed()) + "\n"
	if err := writeNew(*out, []byte(body), 0o600); err != nil {
		return err
	}
	pubB64 := base64.StdEncoding.EncodeToString(pub)
	if err := writeNew(*out+".pub", []byte(pubB64+"\n"), 0o644); err != nil { //nolint:gosec // a public key
		return err
	}
	fmt.Fprintf(stdout, "private key: %s (keep it on this machine, back it up offline)\npublic key:  %s\nkey id:      %s\n\n"+
		"build nodes with\n  -ldflags \"-X topcoat/node/internal/version.UpdateKeys=%s\"\n",
		*out, pubB64, update.KeyID(pub), pubB64)
	return nil
}

// writeNew creates a file and refuses to overwrite one: a key must never be replaced by accident.
func writeNew(path string, data []byte, mode os.FileMode) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode) //nolint:gosec // chosen by the operator
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		return err
	}
	return f.Close()
}

func loadKey(path string) (ed25519.PrivateKey, error) {
	fi, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if runtime.GOOS != "windows" && fi.Mode().Perm()&0o077 != 0 {
		return nil, fmt.Errorf("%s is readable by others (mode %o); chmod 600 it", path, fi.Mode().Perm())
	}
	raw, err := os.ReadFile(path) //nolint:gosec // chosen by the operator
	if err != nil {
		return nil, err
	}
	head, seed, ok := strings.Cut(strings.TrimSpace(string(raw)), " ")
	if !ok || head != keyHeader {
		return nil, fmt.Errorf("%s is not a topcoat-sign key file", path)
	}
	b, err := base64.StdEncoding.DecodeString(seed)
	if err != nil || len(b) != ed25519.SeedSize {
		return nil, fmt.Errorf("%s: the key is damaged", path)
	}
	return ed25519.NewKeyFromSeed(b), nil
}

// --- sign ---------------------------------------------------------------------

type signFlags struct {
	key, binary, version, osName, arch, channel, outDir *string
}

func (s *signFlags) register(fs *flag.FlagSet) {
	s.key = fs.String("key", "", "private key file from keygen")
	s.binary = fs.String("binary", "", "the topcoat-node program to publish")
	s.version = fs.String("version", "", "version, e.g. 0.2.0")
	s.osName = fs.String("os", "", "target OS (linux, windows)")
	s.arch = fs.String("arch", "", "target architecture (amd64, arm64)")
	s.channel = fs.String("channel", "stable", "update channel")
	s.outDir = fs.String("out-dir", "", "where manifest.json and manifest.sig go (default: next to the binary)")
}

// bundle is a signed build on disk.
type bundle struct{ dir, binary string }

func (b bundle) manifest() string { return filepath.Join(b.dir, update.ManifestFile) }
func (b bundle) sig() string      { return filepath.Join(b.dir, update.SigFile) }

func (s *signFlags) run(stdout io.Writer) (bundle, error) {
	for name, v := range map[string]string{"key": *s.key, "binary": *s.binary, "version": *s.version, "os": *s.osName, "arch": *s.arch} {
		if v == "" {
			return bundle{}, fmt.Errorf("sign: --%s is required", name)
		}
	}
	priv, err := loadKey(*s.key)
	if err != nil {
		return bundle{}, err
	}
	bin, err := os.ReadFile(*s.binary) //nolint:gosec // chosen by the operator
	if err != nil {
		return bundle{}, err
	}
	m := update.Manifest{
		Format: 1, Product: update.Product, Channel: *s.channel, Version: *s.version,
		OS: *s.osName, Arch: *s.arch, Size: int64(len(bin)), SHA256: update.SHA256Hex(bin),
		Created: time.Now().UTC().Format(time.RFC3339),
	}
	if err := m.Validate(); err != nil {
		return bundle{}, err
	}
	mb, err := json.Marshal(m)
	if err != nil {
		return bundle{}, err
	}
	dir := *s.outDir
	if dir == "" {
		dir = filepath.Dir(*s.binary)
	}
	if err := os.MkdirAll(dir, 0o755); err != nil { //nolint:gosec // release output
		return bundle{}, err
	}
	b := bundle{dir: dir, binary: *s.binary}
	if err := os.WriteFile(b.manifest(), mb, 0o644); err != nil { //nolint:gosec // public
		return bundle{}, err
	}
	if err := os.WriteFile(b.sig(), []byte(update.Sign(priv, mb)+"\n"), 0o644); err != nil { //nolint:gosec // public
		return bundle{}, err
	}
	fmt.Fprintf(stdout, "signed %s %s for %s/%s on channel %s\n  %s\n  %s\n  sha256 %s (%d bytes)\n",
		update.Product, m.Version, m.OS, m.Arch, m.Channel, b.manifest(), b.sig(), m.SHA256, m.Size)
	return b, nil
}

func cmdSign(args []string, stdout io.Writer) (bundle, error) {
	fs := newFlags("sign")
	var sf signFlags
	sf.register(fs)
	if err := fs.Parse(args); err != nil {
		return bundle{}, err
	}
	return sf.run(stdout)
}

// --- upload -------------------------------------------------------------------

type uploadFlags struct {
	hub, dir, binary, fingerprint, caFile *string
	insecureHTTP                          *bool
}

func (u *uploadFlags) register(fs *flag.FlagSet, withFiles bool) {
	u.hub = fs.String("hub", "", "hub URL, e.g. https://hub.example.lan:8443")
	u.fingerprint = fs.String("ca-fingerprint", "", "pin the hub CA, sha256:<hex>")
	u.caFile = fs.String("ca-file", "", "trust this CA certificate (PEM) instead of the system store")
	u.insecureHTTP = fs.Bool("insecure-http", false, "allow a plain http:// hub URL (tests and local development only)")
	if withFiles {
		u.dir = fs.String("dir", "", "directory with manifest.json and manifest.sig")
		u.binary = fs.String("binary", "", "the signed program (default: <dir>/topcoat-node)")
	}
}

func (u *uploadFlags) client() (*http.Client, error) {
	if *u.hub == "" {
		return nil, errors.New("--hub is required")
	}
	useTLS, err := hubtls.CheckScheme(*u.hub, *u.insecureHTTP)
	if err != nil {
		return nil, err
	}
	if !useTLS {
		return hubtls.Client(nil, 0), nil
	}
	var cfg *tls.Config
	switch {
	case *u.fingerprint != "":
		fp, err := hubtls.ParseFingerprint(*u.fingerprint)
		if err != nil {
			return nil, err
		}
		parsed, err := url.Parse(*u.hub)
		if err != nil {
			return nil, err
		}
		cfg, _ = hubtls.PinnedConfig(fp, parsed.Hostname())
	case *u.caFile != "":
		pem, err := os.ReadFile(*u.caFile)
		if err != nil {
			return nil, err
		}
		if cfg, err = hubtls.RootsConfig(pem); err != nil {
			return nil, err
		}
	default:
		cfg = hubtls.SystemConfig()
	}
	return hubtls.Client(cfg, 0), nil
}

func (u *uploadFlags) run(ctx context.Context, b bundle, stdout io.Writer) error {
	token := strings.TrimSpace(os.Getenv(tokenEnv))
	if token == "" {
		return fmt.Errorf("set $%s to the hub admin token", tokenEnv)
	}
	client, err := u.client()
	if err != nil {
		return err
	}
	mb, err := os.ReadFile(b.manifest()) //nolint:gosec // chosen by the operator
	if err != nil {
		return err
	}
	sig, err := os.ReadFile(b.sig()) //nolint:gosec // chosen by the operator
	if err != nil {
		return err
	}
	m, err := update.ParseManifest(mb)
	if err != nil {
		return err
	}
	f, err := os.Open(b.binary) //nolint:gosec // chosen by the operator
	if err != nil {
		return err
	}
	defer f.Close() //nolint:errcheck // read only
	fi, err := f.Stat()
	if err != nil {
		return err
	}
	if fi.Size() != m.Size {
		return fmt.Errorf("%s is %d bytes, the manifest says %d: sign again", b.binary, fi.Size(), m.Size)
	}
	base := strings.TrimRight(*u.hub, "/") + "/api/updates/" + url.PathEscape(m.Channel) + "/" +
		url.PathEscape(m.Version) + "/" + url.PathEscape(m.OS+"-"+m.Arch)
	body, err := json.Marshal(map[string]string{"manifest": string(mb), "signature": strings.TrimSpace(string(sig))})
	if err != nil {
		return err
	}
	if err := put(ctx, client, base+"/manifest", token, "application/json", bytes.NewReader(body), int64(len(body))); err != nil {
		return fmt.Errorf("upload the manifest: %w", err)
	}
	if err := put(ctx, client, base+"/binary", token, "application/octet-stream", f, fi.Size()); err != nil {
		return fmt.Errorf("upload the program: %w", err)
	}
	fmt.Fprintf(stdout, "published %s %s (%s/%s) on channel %s\n", update.Product, m.Version, m.OS, m.Arch, m.Channel)
	return nil
}

func put(ctx context.Context, c *http.Client, target, token, ctype string, body io.Reader, size int64) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, target, body)
	if err != nil {
		return err
	}
	req.ContentLength = size
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", ctype)
	resp, err := c.Do(req)
	if err != nil {
		return hubtls.Explain(err)
	}
	defer resp.Body.Close() //nolint:errcheck // read only
	if resp.StatusCode/100 != 2 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<10))
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(msg)))
	}
	return nil
}

func cmdUpload(ctx context.Context, args []string, stdout io.Writer) error {
	fs := newFlags("upload")
	var uf uploadFlags
	uf.register(fs, true)
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *uf.dir == "" {
		return errors.New("upload: --dir is required")
	}
	b := bundle{dir: *uf.dir, binary: *uf.binary}
	if b.binary == "" {
		b.binary = filepath.Join(b.dir, update.BinaryFile)
	}
	return uf.run(ctx, b, stdout)
}

func cmdPublish(ctx context.Context, args []string, stdout io.Writer) error {
	fs := newFlags("publish")
	var sf signFlags
	var uf uploadFlags
	sf.register(fs)
	uf.register(fs, false)
	if err := fs.Parse(args); err != nil {
		return err
	}
	b, err := sf.run(stdout)
	if err != nil {
		return err
	}
	return uf.run(ctx, b, stdout)
}

// --- verify -------------------------------------------------------------------

func cmdVerify(args []string, stdout io.Writer) error {
	fs := newFlags("verify")
	pub := fs.String("pub", "", "public key (base64) or a file holding it")
	dir := fs.String("dir", "", "directory with manifest.json, manifest.sig and the program")
	binary := fs.String("binary", "", "the program (default: <dir>/topcoat-node)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *pub == "" || *dir == "" {
		return errors.New("verify: --pub and --dir are required")
	}
	keyText := *pub
	if raw, err := os.ReadFile(*pub); err == nil { //nolint:gosec // chosen by the operator
		keyText = strings.TrimSpace(string(raw))
	}
	keys, err := update.ParseKeys(keyText)
	if err != nil {
		return err
	}
	bin := *binary
	if bin == "" {
		bin = filepath.Join(*dir, update.BinaryFile)
	}
	mb, err := os.ReadFile(filepath.Join(*dir, update.ManifestFile)) //nolint:gosec // chosen by the operator
	if err != nil {
		return err
	}
	sig, err := os.ReadFile(filepath.Join(*dir, update.SigFile)) //nolint:gosec // chosen by the operator
	if err != nil {
		return err
	}
	data, err := os.ReadFile(bin) //nolint:gosec // chosen by the operator
	if err != nil {
		return err
	}
	sigLine := strings.TrimSpace(string(sig))
	if err := update.Verify(keys, mb, sigLine); err != nil {
		return err
	}
	m, err := update.ParseManifest(mb)
	if err != nil {
		return err
	}
	if int64(len(data)) != m.Size || update.SHA256Hex(data) != m.SHA256 {
		return errors.New("the program does not match the size and SHA-256 of the manifest")
	}
	fmt.Fprintf(stdout, "ok: %s %s %s/%s on channel %s\n", update.Product, m.Version, m.OS, m.Arch, m.Channel)
	return nil
}
