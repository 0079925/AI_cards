Import-Module Posh-SSH

$Server = "2.26.108.37"
$User = "root"
$Password = "CHANGE_ME"
$RemoteDir = "/opt/lead-intake-n8n"

$sec = ConvertTo-SecureString $Password -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential ($User, $sec)
$session = New-SSHSession -ComputerName $Server -Credential $cred -AcceptKey

Invoke-SSHCommand -SSHSession $session -Command "mkdir -p $RemoteDir"
Set-SCPItem -SSHSession $session -Path "./*" -Destination $RemoteDir -Recurse
Invoke-SSHCommand -SSHSession $session -Command "cd $RemoteDir && cp -n .env.example .env && docker compose up -d"
Invoke-SSHCommand -SSHSession $session -Command "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"

Remove-SSHSession -SSHSession $session
