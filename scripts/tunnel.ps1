<#
.SYNOPSIS
  Expose the Chief voice page to your phone over HTTPS with a Cloudflare quick tunnel.

.DESCRIPTION
  Phones only allow microphone access on HTTPS pages, so localhost over Wi-Fi is not enough.
  Start the page first (python -m chief web --port 8765), then run this script. cloudflared prints
  a https://<random>.trycloudflare.com address; open it on the phone with ?token=<CHIEF_WEB_TOKEN>.

  cloudflared is not bundled. Install it once with:
      winget install --id Cloudflare.cloudflared
  then open a new terminal so it is on PATH.

.EXAMPLE
  .\scripts\tunnel.ps1 -Port 8765
#>
param(
  [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
  Write-Host "cloudflared is not installed." -ForegroundColor Yellow
  Write-Host "Install it with:  winget install --id Cloudflare.cloudflared"
  Write-Host "Then open a new terminal and run this script again."
  exit 1
}

Write-Host "Tunnelling http://localhost:$Port ..."
Write-Host "Look for the https://*.trycloudflare.com line below, then open it on your phone as:"
Write-Host "  https://<that-host>/?token=<your CHIEF_WEB_TOKEN>"
Write-Host "Anyone with the full link can reach the page, so do not share or screenshot it."
Write-Host ""

cloudflared tunnel --url "http://localhost:$Port"
