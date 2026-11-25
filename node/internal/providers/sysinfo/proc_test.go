package sysinfo

import "testing"

const cpuinfo = `processor	: 0
vendor_id	: AuthenticAMD
model name	: Fake Ryzen 8-Core Processor
cpu cores	: 8

processor	: 1
vendor_id	: AuthenticAMD
model name	: Fake Ryzen 8-Core Processor
`

const meminfo = `MemTotal:       32768000 kB
MemFree:         1000000 kB
MemAvailable:   24576000 kB
Buffers:          100000 kB
Cached:          2000000 kB
`

func TestParseCPUInfo(t *testing.T) {
	model, cores := parseCPUInfo([]byte(cpuinfo))
	if model != "Fake Ryzen 8-Core Processor" || cores != 2 {
		t.Fatalf("got %q %d", model, cores)
	}
}

// the figures of a Linux node with an 18 GB model mapped by llama-server (measured, rounded)
const meminfoWithModel = `MemTotal:       31754048 kB
MemFree:         4084144 kB
MemAvailable:   24800852 kB
Cached:         21054668 kB
Mapped:         15661320 kB
Shmem:             91548 kB
`

func TestParseMemInfoCountsMappedModelFiles(t *testing.T) {
	total, used, err := parseMemInfo([]byte(meminfoWithModel))
	if err != nil {
		t.Fatal(err)
	}
	// 31754048 - 24800852 + (15661320 - 91548) kB = 22522968 kB
	if total != 31009 || used != 21995 {
		t.Fatalf("got total=%d used=%d", total, used)
	}
	if pct := used * 100 / total; pct < 65 || pct > 75 {
		t.Fatalf("a mapped model counts as in use, got %d %%", pct)
	}
}

func TestParseMemInfoNeverExceedsTotalOrGoesNegative(t *testing.T) {
	_, used, _ := parseMemInfo([]byte("MemTotal: 2048000 kB\nMemAvailable: 100000 kB\nMapped: 4000000 kB\n"))
	if used != 2000 {
		t.Fatalf("capped at the total, got %d", used)
	}
	_, used, _ = parseMemInfo([]byte("MemTotal: 2048000 kB\nMemAvailable: 2048000 kB\nMapped: 10 kB\nShmem: 500 kB\n"))
	if used != 0 {
		t.Fatalf("shared memory larger than mapped adds nothing, got %d", used)
	}
}

func TestParseMemInfo(t *testing.T) {
	total, used, err := parseMemInfo([]byte(meminfo))
	if err != nil {
		t.Fatal(err)
	}
	if total != 32000 || used != 8000 {
		t.Fatalf("got total=%d used=%d", total, used)
	}
	if _, _, err := parseMemInfo([]byte("Foo: 1 kB\n")); err == nil {
		t.Fatal("expected error without MemTotal")
	}
	// Fallback without MemAvailable.
	total, used, err = parseMemInfo([]byte("MemTotal: 2048 kB\nMemFree: 1024 kB\n"))
	if err != nil || total != 2 || used != 1 {
		t.Fatalf("fallback got %d %d %v", total, used, err)
	}
}
