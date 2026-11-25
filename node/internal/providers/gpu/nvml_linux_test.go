package gpu

import (
	"encoding/binary"
	"math"
	"testing"
)

func TestParseNVMLBuffers(t *testing.T) {
	buf := make([]byte, 3*nvmlProcInfoSize)
	put := func(i int, pid uint32, used uint64) {
		binary.LittleEndian.PutUint32(buf[i*nvmlProcInfoSize:], pid)
		binary.LittleEndian.PutUint64(buf[i*nvmlProcInfoSize+8:], used)
	}
	put(0, 100, 5<<30)
	put(1, 200, 1<<30)
	put(2, 300, math.MaxUint64) // not available
	got := parseProcInfos(buf, 3)
	if len(got) != 2 || got[100] != 5<<30 || got[200] != 1<<30 {
		t.Fatalf("%v", got)
	}
	if got := parseProcInfos(buf, 10); len(got) != 2 { // n larger than the buffer
		t.Fatalf("%v", got)
	}

	ub := make([]byte, 2*nvmlUtilSampleSize)
	binary.LittleEndian.PutUint32(ub[0:], 100)
	binary.LittleEndian.PutUint32(ub[16:], 40)
	binary.LittleEndian.PutUint32(ub[nvmlUtilSampleSize:], 100)
	binary.LittleEndian.PutUint32(ub[nvmlUtilSampleSize+16:], 70)
	if u := parseUtilSamples(ub, 2); u[100] != 70 {
		t.Fatalf("%v", u)
	}
}
