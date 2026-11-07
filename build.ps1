$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "[1/3] 安装运行依赖..." -ForegroundColor Cyan
python -m pip install --disable-pip-version-check -r requirements.txt

Write-Host "[2/3] 安装打包工具..." -ForegroundColor Cyan
python -m pip install --disable-pip-version-check pyinstaller

Write-Host "[3/3] 构建单文件 exe..." -ForegroundColor Cyan
python -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name "上班鼓励器" `
  --icon "assets/app.ico" `
  --add-data "index.html;." `
  main.py

Write-Host ""
Write-Host "构建完成：dist\上班鼓励器.exe" -ForegroundColor Green
