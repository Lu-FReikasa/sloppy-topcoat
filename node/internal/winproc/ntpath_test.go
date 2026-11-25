package winproc

import "testing"

func TestNTToDosPath(t *testing.T) {
	drives := map[string]string{
		"C:": `\Device\HarddiskVolume3`,
		"D:": `\Device\HarddiskVolume13`,
	}
	cases := map[string]string{
		`\Device\HarddiskVolume3\Windows\explorer.exe`:                      `C:\Windows\explorer.exe`,
		`\Device\HarddiskVolume13\SteamLibrary\steamapps\common\Game\g.exe`: `D:\SteamLibrary\steamapps\common\Game\g.exe`,
		`\device\harddiskvolume13\x.exe`:                                    `D:\x.exe`,
		`\Device\Mup\nas\games\x.exe`:                                       `\\nas\games\x.exe`,
		`\??\C:\Tools\y.exe`:                                                `C:\Tools\y.exe`,
		`\Device\HarddiskVolume7\z.exe`:                                     "",
	}
	for in, want := range cases {
		if got := NTToDosPath(in, drives); got != want {
			t.Errorf("%s: got %q want %q", in, got, want)
		}
	}
	// HarddiskVolume1 must not match HarddiskVolume13.
	if got := NTToDosPath(`\Device\HarddiskVolume13\a.exe`, map[string]string{"E:": `\Device\HarddiskVolume1`}); got != "" {
		t.Errorf("prefix confusion: %q", got)
	}
}
