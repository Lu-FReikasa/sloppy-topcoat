package activity

import (
	"encoding/binary"
	"testing"
	"time"
)

func wtsBuffer(state uint32, last, cur int64) []byte {
	b := make([]byte, wtsInfoSize)
	binary.LittleEndian.PutUint32(b[wtsInfoStateOffset:], state)
	binary.LittleEndian.PutUint64(b[wtsInfoLastInputAt:], uint64(last))
	binary.LittleEndian.PutUint64(b[wtsInfoCurrentTimeAt:], uint64(cur))
	return b
}

func TestIdleFromWTSInfo(t *testing.T) {
	const second = 10_000_000 // 100 ns units
	base := int64(133_000_000_000_000_000)
	if d, ok := idleFromWTSInfo(wtsBuffer(0, base, base+90*second)); !ok || d != 90*time.Second {
		t.Fatalf("%v %v", d, ok)
	}
	if d, ok := idleFromWTSInfo(wtsBuffer(0, base+5, base)); !ok || d != 0 {
		t.Fatalf("skew: %v %v", d, ok)
	}
	if _, ok := idleFromWTSInfo(wtsBuffer(0, 0, base)); ok {
		t.Fatal("no input yet is unknown")
	}
	if _, ok := idleFromWTSInfo(make([]byte, 10)); ok {
		t.Fatal("short buffer is unknown")
	}
	if wtsState(wtsBuffer(4, 1, 1)) != 4 || wtsState(nil) != 0xFFFFFFFF {
		t.Fatal("state")
	}
}
