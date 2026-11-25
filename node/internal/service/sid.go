package service

import (
	"crypto/sha1" //nolint:gosec // the SID scheme is defined with SHA-1; not used for security
	"encoding/binary"
	"strconv"
	"strings"
	"unicode/utf16"
)

// Account is the Windows virtual service account the node runs as (D17).
const Account = `NT SERVICE\` + Name

// PerformanceMonitorUsersSID is the builtin group whose members may read
// performance counters (the GPU memory/engine counters, D17).
const PerformanceMonitorUsersSID = "S-1-5-32-558"

// AccountSID returns the SID of a service's virtual account: S-1-5-80
// followed by the SHA-1 of the upper-cased service name (UTF-16LE) as five
// little-endian 32-bit values (what `sc showsid` prints). It does not need
// the service to exist, so uninstall can still find the account.
func AccountSID(service string) string {
	u := utf16.Encode([]rune(strings.ToUpper(service)))
	b := make([]byte, 2*len(u))
	for i, c := range u {
		binary.LittleEndian.PutUint16(b[2*i:], c)
	}
	sum := sha1.Sum(b) //nolint:gosec // see import
	parts := []string{"S-1-5-80"}
	for i := 0; i < 5; i++ {
		parts = append(parts, strconv.FormatUint(uint64(binary.LittleEndian.Uint32(sum[4*i:])), 10))
	}
	return strings.Join(parts, "-")
}

// dataDirSDDLFor grants full control to SYSTEM, Administrators and the
// service's virtual account; inheritance from %ProgramData% is cut off so
// normal users cannot read the node key. An empty sid leaves the account out
// (used by uninstall without --purge).
func dataDirSDDLFor(sid string) string {
	s := "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
	if sid != "" {
		s += "(A;OICI;FA;;;" + sid + ")"
	}
	return s
}

// stateDirSDDLFor is the ACL of the updater's own directory: full control for SYSTEM and
// Administrators, read-only for the node's virtual account (it reads the heartbeat and the result
// but cannot create, replace or link anything there). Inheritance from %ProgramData% is cut off.
func stateDirSDDLFor(sid string) string {
	return "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FRFX;;;" + sid + ")"
}
