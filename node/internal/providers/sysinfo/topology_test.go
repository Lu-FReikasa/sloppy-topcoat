package sysinfo

import (
	"encoding/binary"
	"fmt"
	"testing"
)

// record builds one RelationProcessorCore record.
func record(class byte, masks ...uint64) []byte {
	size := 32 + 16*len(masks)
	b := make([]byte, size)
	binary.LittleEndian.PutUint32(b[0:], 0)
	binary.LittleEndian.PutUint32(b[4:], uint32(size))
	b[9] = class
	binary.LittleEndian.PutUint16(b[30:], uint16(len(masks)))
	for i, m := range masks {
		binary.LittleEndian.PutUint64(b[32+16*i:], m)
	}
	return b
}

func TestParseProcessorCoresHybrid(t *testing.T) {
	// i7-1255U: 2 P-cores with HT (class 1), 8 E-cores (class 0).
	var buf []byte
	buf = append(buf, record(1, 0b11)...)
	buf = append(buf, record(1, 0b1100)...)
	for i := 0; i < 8; i++ {
		buf = append(buf, record(0, 1<<(4+i))...)
	}
	buf = append(buf, 0, 0, 0) // trailing garbage is ignored
	phys, threads, p, e := summarize(parseProcessorCores(buf))
	if got := fmt.Sprint(phys, threads, p, e); got != "10 12 2 8" {
		t.Fatalf("got %s", got)
	}
}

func TestParseProcessorCoresUniform(t *testing.T) {
	var buf []byte
	for i := 0; i < 8; i++ {
		buf = append(buf, record(0, 0b11<<(2*i))...)
	}
	phys, threads, p, e := summarize(parseProcessorCores(buf))
	if got := fmt.Sprint(phys, threads, p, e); got != "8 16 8 0" {
		t.Fatalf("got %s", got)
	}
}

func TestParseCPUList(t *testing.T) {
	m := parseCPUList("0-3,8, 10-11,x,5-4")
	if len(m) != 7 || !m[0] || !m[3] || !m[8] || !m[11] || m[4] {
		t.Fatalf("%v", m)
	}
}
