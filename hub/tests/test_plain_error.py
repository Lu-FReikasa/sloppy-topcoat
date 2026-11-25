"""A Windows node's PowerShell error reaches the model and the report as one line (live
2026-11-09: a German CLIXML block was shown to the model and in the card, and misread)."""

from topcoat_hub.agent_prompts import plain_error

CLIXML = (
    "powershell failed: exit status 1: #< CLIXML\n"
    '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
    '<S S="Error">Get-WinEvent : Auf dem Computer "localhost" wurde kein Ereignisanbieter '
    'gefunden, der "audio" _x000D__x000A_</S><S S="Error">entspricht._x000D__x000A_</S>'
    '<S S="Error">In Zeile:1 Zeichen:589_x000D__x000A_</S>'
    '<S S="Error">+ ... T};try{$e=@(Get-WinEvent -FilterHashtable $f)}catch{ ..._x000D__x000A_</S>'
    '<S S="Error">    + CategoryInfo          : ObjectNotFound: (audio:String) [Get-WinEvent], '
    "Exception_x000D__x000A_</S>"
    '<S S="Error">    + FullyQualifiedErrorId : NoMatchingProvidersFound,Microsoft.PowerShell.'
    'Commands.GetWinEventC _x000D__x000A_</S><S S="Error">   ommand_x000D__x000A_</S></Objs>'
)


def test_a_clixml_error_becomes_its_message_and_its_error_id() -> None:
    line = plain_error(CLIXML)
    assert line == (
        'powershell failed: exit status 1: Get-WinEvent : Auf dem Computer "localhost" wurde '
        'kein Ereignisanbieter gefunden, der "audio" entspricht. [NoMatchingProvidersFound]'
    )
    assert "\n" not in line and "<S" not in line and "_x000D_" not in line


def test_other_errors_stay_as_they_are() -> None:
    assert plain_error("not_found: no such container") == "not_found: no such container"
    assert len(plain_error(CLIXML, limit=40)) == 40
