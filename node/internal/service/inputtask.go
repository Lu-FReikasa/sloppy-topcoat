package service

import (
	"bytes"
	"encoding/xml"
	"unicode/utf16"
)

// InputHelperTask is the name of the scheduled task that starts the input helper (D24) in the
// session of whoever logs on.
const InputHelperTask = "topcoat-node-input"

// usersSID is the builtin group of ordinary accounts. A task whose principal is a group runs in
// the session of the user who triggered it, "only when the user is logged on", with that user's
// own rights and without any stored password (S4U fails in a Samba domain). A group principal has
// no LogonType: schtasks refuses one next to a GroupId ("unexpected node", seen on the real
// laptop), the group already means "the interactive session of a member".
const usersSID = "S-1-5-32-545"

// InputHelperTaskXML is the task definition: at logon of any user, start
// `<exe> input-helper` with the rights of that user (a group principal: their interactive
// session, no elevation, no password), hidden, once per session, without a time limit and
// regardless of battery. The elements are the ones, in the order, that Task Scheduler itself
// exports for such a task; schtasks reports only its first complaint, so nothing is added to it.
func InputHelperTaskXML(exe string) string {
	var esc bytes.Buffer
	_ = xml.EscapeText(&esc, []byte(exe))
	return `<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Tells the topcoat-node service how long ago the keyboard or mouse was last used in this session (input helper). It only answers the service over a local named pipe.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Users">
      <GroupId>` + usersSID + `</GroupId>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Users">
    <Exec>
      <Command>` + esc.String() + `</Command>
      <Arguments>input-helper</Arguments>
    </Exec>
  </Actions>
</Task>
`
}

// utf16LEWithBOM is the encoding schtasks /XML reads without complaint.
func utf16LEWithBOM(s string) []byte {
	u := utf16.Encode([]rune(s))
	b := make([]byte, 0, 2+2*len(u))
	b = append(b, 0xFF, 0xFE)
	for _, c := range u {
		b = append(b, byte(c&0xFF), byte(c>>8)) // low byte, high byte
	}
	return b
}
