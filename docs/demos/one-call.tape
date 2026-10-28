# One real `yojo` call, recorded against the test fixture Jira (no network, no OS keyring).
# Regenerate with `python scripts/capture_demos.py --write`; never edit the outputs by hand.
# capture_demos.py adds the Output lines and puts a fixture-backed `yojo` on PATH.

Set Shell "bash"
Set FontFamily "JetBrains Mono"
Set FontSize 18
Set Width 1380
Set Height 400
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms
Set CursorBlink false
Env LANG "C.UTF-8"
Env LC_ALL "C.UTF-8"

Hide
Type "export PS1='$ ' HISTFILE=/dev/null && clear"
Enter
Sleep 300ms
Show

Type 'yojo jira issue create PROJ --type Bug --summary "Fix example sign-in" --base-url https://jira.example.test/jira'
Sleep 400ms
Enter
Sleep 4s
