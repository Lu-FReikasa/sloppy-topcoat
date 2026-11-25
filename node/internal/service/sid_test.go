package service

import (
	"strings"
	"testing"
)

func TestServiceSIDMatchesWindows(t *testing.T) {
	// Well-known SID of the TrustedInstaller service account.
	if got := AccountSID("TrustedInstaller"); got != "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464" {
		t.Fatalf("TrustedInstaller: %s", got)
	}
	if AccountSID("topcoat-node") != AccountSID("TOPCOAT-NODE") {
		t.Fatal("service names are case-insensitive")
	}
}

func TestDataDirSDDL(t *testing.T) {
	if got := dataDirSDDLFor("S-1-5-80-1-2-3-4-5"); got != "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;S-1-5-80-1-2-3-4-5)" {
		t.Fatal(got)
	}
	if got := dataDirSDDLFor(""); got != "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)" {
		t.Fatal(got)
	}
}

func TestStateDirIsReadOnlyForTheNodeAccount(t *testing.T) {
	sddl := stateDirSDDLFor(AccountSID(Name))
	if !strings.Contains(sddl, "(A;OICI;FRFX;;;"+AccountSID(Name)+")") || strings.Contains(sddl, "FA;;;"+AccountSID(Name)) {
		t.Fatalf("sddl = %s", sddl)
	}
	if !strings.HasPrefix(sddl, "D:P(") {
		t.Fatalf("inheritance is not cut off: %s", sddl)
	}
}
