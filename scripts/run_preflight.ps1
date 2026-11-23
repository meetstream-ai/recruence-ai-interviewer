# Asks for the admin email and the test role, then runs the B-1 pre-flight check against the desktop app
# (scripts/live_preflight.py asks for the password itself and never prints or stores it).
$email = Read-Host "Admin email for Recruence"
$role = Read-Host "Exact title of the test role"
& "$PSScriptRoot\..\.venv\Scripts\python.exe" "$PSScriptRoot\live_preflight.py" --url http://127.0.0.1:8765 --email $email --role $role
