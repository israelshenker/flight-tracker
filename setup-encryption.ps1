# One-time setup for encrypted fares. Run it yourself in a terminal:
#   powershell -ExecutionPolicy Bypass -File "C:\Users\israe\Dropbox\Flight Tool\setup-encryption.ps1"
# It asks for your passphrase (hidden), encrypts the data files in this folder, and saves
# the passphrase plus your email/push settings as secrets on the new GitHub repo.
# Nothing you type is shown on screen or saved in any file.
param([string]$Repo = "israelshenker/flight-tracker")
$ErrorActionPreference = "Stop"
$gh = "C:\Program Files\GitHub CLI\gh.exe"
Set-Location $PSScriptRoot

function Read-Secret($prompt) {
    $secure = Read-Host $prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

Write-Host ""
Write-Host "Choose a passphrase for your fare page. At least 12 characters; a few random words works well."
Write-Host "Anyone you give it to can see your fares. If you lose it, the saved price history can't be recovered."
$p1 = Read-Secret "Passphrase"
$p2 = Read-Secret "Type it again"
if ($p1 -ne $p2) { Write-Host "The two didn't match. Nothing was changed; run the script again."; exit 1 }
if ($p1.Length -lt 12) { Write-Host "That's shorter than 12 characters. Nothing was changed; run the script again."; exit 1 }

$env:DATA_KEY = $p1
python vault.py encrypt
if ($LASTEXITCODE -ne 0) { Remove-Item Env:DATA_KEY; Write-Host "Encryption failed; see the message above."; exit 1 }

$p1 | & $gh secret set DATA_KEY -R $Repo
Remove-Item Env:DATA_KEY
$p1 = $null; $p2 = $null
Write-Host "Saved the passphrase as the DATA_KEY secret."

Write-Host ""
Write-Host "Now your alert settings, for the new repo (same values as before)."
foreach ($name in "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "NTFY_TOPIC") {
    $value = Read-Secret $name
    if ($value) { $value | & $gh secret set $name -R $Repo; Write-Host "Saved $name." }
    else { Write-Host "Skipped $name (left empty)." }
}
Write-Host ""
Write-Host "Done. Tell Claude the script finished."
