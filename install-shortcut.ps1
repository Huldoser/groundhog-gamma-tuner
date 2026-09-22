#Requires -Version 5.1
$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$pngPath = Join-Path $root 'assets\app_icon.png'
$icoPath = Join-Path $root 'assets\app_icon.ico'
$batPath = Join-Path $root 'run.bat'

if (-not (Test-Path -LiteralPath $pngPath)) {
  throw "Missing icon: $pngPath"
}
if (-not (Test-Path -LiteralPath $batPath)) {
  throw "Missing launcher: $batPath"
}

Add-Type -AssemblyName System.Drawing

function Write-UInt16LE {
  param(
    [System.IO.BinaryWriter]$Writer,
    [int]$Value
  )
  $Writer.Write([byte]($Value -band 0xFF))
  $Writer.Write([byte](($Value -shr 8) -band 0xFF))
}

function Write-UInt32LE {
  param(
    [System.IO.BinaryWriter]$Writer,
    [int]$Value
  )
  $Writer.Write([byte]($Value -band 0xFF))
  $Writer.Write([byte](($Value -shr 8) -band 0xFF))
  $Writer.Write([byte](($Value -shr 16) -band 0xFF))
  $Writer.Write([byte](($Value -shr 24) -band 0xFF))
}

function Convert-PngToIco {
  param(
    [string]$SourcePath,
    [string]$DestinationPath
  )

  $sizes = @(16, 32, 48, 64, 128, 256)
  $source = [System.Drawing.Image]::FromFile($SourcePath)
  $images = New-Object System.Collections.Generic.List[object]
  try {
    foreach ($size in $sizes) {
      $bitmap = New-Object System.Drawing.Bitmap $size, $size
      try {
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        try {
          $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
          $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
          $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
          $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
          $graphics.Clear([System.Drawing.Color]::Transparent)
          $graphics.DrawImage($source, 0, 0, $size, $size)
        } finally {
          $graphics.Dispose()
        }

        $stream = New-Object System.IO.MemoryStream
        try {
          $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
          $images.Add(@{
            Size = $size
            Bytes = $stream.ToArray()
          })
        } finally {
          $stream.Dispose()
        }
      } finally {
        $bitmap.Dispose()
      }
    }
  } finally {
    $source.Dispose()
  }

  $count = $images.Count
  $offset = 6 + (16 * $count)
  $file = [System.IO.File]::Create($DestinationPath)
  $writer = New-Object System.IO.BinaryWriter $file
  try {
    Write-UInt16LE $writer 0
    Write-UInt16LE $writer 1
    Write-UInt16LE $writer $count
    foreach ($image in $images) {
      $side = 0
      if ($image.Size -lt 256) {
        $side = $image.Size
      }
      $writer.Write([byte]$side)
      $writer.Write([byte]$side)
      $writer.Write([byte]0)
      $writer.Write([byte]0)
      Write-UInt16LE $writer 1
      Write-UInt16LE $writer 32
      Write-UInt32LE $writer $image.Bytes.Length
      Write-UInt32LE $writer $offset
      $offset += $image.Bytes.Length
    }
    foreach ($image in $images) {
      $bytes = [byte[]]$image.Bytes
      $writer.Write($bytes, 0, $bytes.Length)
    }
  } finally {
    $writer.Dispose()
  }
}

Convert-PngToIco -SourcePath $pngPath -DestinationPath $icoPath

$desktop = [Environment]::GetFolderPath('Desktop')
if ([string]::IsNullOrWhiteSpace($desktop)) {
  throw 'Could not find the desktop folder.'
}

$shortcutPath = Join-Path $desktop 'Groundhog Gamma Tuner.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $batPath
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$icoPath,0"
$shortcut.Description = 'Groundhog Gamma Tuner'
$shortcut.Save()

Write-Host "Desktop shortcut created: $shortcutPath"
