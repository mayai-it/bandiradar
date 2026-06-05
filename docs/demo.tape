# BandiRadar demo GIF — render with:  vhs docs/demo.tape
# (run from the repo root)
# Tight crop: the batch table fills the frame, no dead space.
# Produces bandiradar-demo.gif (gitignored).

Output bandiradar-demo.gif

Require uv

Set Shell bash
Set FontSize 16
Set Width 1140
Set Height 300
Set Padding 28
Set Theme "Catppuccin Mocha"
Set TypingSpeed 55ms

# setup (hidden): warm the uv env so the visible command runs instantly + clean
Hide
Type "uv run bandiradar --help >/dev/null 2>&1 && clear"
Enter
Sleep 2s
Show

# the breadth shot: every company type, one command
Type "uv run bandiradar batch --sample"
Sleep 700ms
Enter
Sleep 5s
Sleep 800ms
