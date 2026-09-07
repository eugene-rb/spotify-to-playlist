# Regenerate the Windows icon and preview from the shared WPF vector artwork.
# Run with Windows PowerShell (WPF requires STA).
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName PresentationCore, PresentationFramework, WindowsBase
$AssetDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) "native/PlaylistAudioSaver/Assets"
$Resources = [Windows.Markup.XamlReader]::Parse([IO.File]::ReadAllText((Join-Path $AssetDirectory "AppIcon.xaml")))
$Drawing = $Resources["AppIcon"]
$Sizes = @(16, 20, 24, 32, 40, 48, 64, 128, 256)
$Frames = @()
foreach ($Size in $Sizes + @(512)) {
    $Visual = New-Object Windows.Media.DrawingVisual
    $Context = $Visual.RenderOpen()
    $Context.DrawImage($Drawing, [Windows.Rect]::new(0, 0, $Size, $Size))
    $Context.Close()
    $Bitmap = [Windows.Media.Imaging.RenderTargetBitmap]::new($Size, $Size, 96, 96, [Windows.Media.PixelFormats]::Pbgra32)
    $Bitmap.Render($Visual)
    $Encoder = New-Object Windows.Media.Imaging.PngBitmapEncoder
    $Encoder.Frames.Add([Windows.Media.Imaging.BitmapFrame]::Create($Bitmap))
    $Stream = New-Object IO.MemoryStream
    try {
        $Encoder.Save($Stream)
        $Bytes = $Stream.ToArray()
        if ($Size -eq 512) {
            [IO.File]::WriteAllBytes((Join-Path $AssetDirectory "AppIcon.png"), $Bytes)
        } else {
            $Frames += ,$Bytes
        }
    } finally { $Stream.Dispose() }
}
$File = [IO.File]::Create((Join-Path $AssetDirectory "AppIcon.ico"))
$Writer = [IO.BinaryWriter]::new($File)
try {
    $Writer.Write([uint16]0)
    $Writer.Write([uint16]1)
    $Writer.Write([uint16]$Sizes.Count)
    $Offset = 6 + 16 * $Sizes.Count
    for ($Index = 0; $Index -lt $Sizes.Count; $Index++) {
        $Dimension = if ($Sizes[$Index] -eq 256) { 0 } else { $Sizes[$Index] }
        $Writer.Write([byte]$Dimension)
        $Writer.Write([byte]$Dimension)
        $Writer.Write([byte]0)
        $Writer.Write([byte]0)
        $Writer.Write([uint16]1)
        $Writer.Write([uint16]32)
        $Writer.Write([uint32]$Frames[$Index].Length)
        $Writer.Write([uint32]$Offset)
        $Offset += $Frames[$Index].Length
    }
    foreach ($Frame in $Frames) { $Writer.Write([byte[]]$Frame) }
} finally { $Writer.Dispose() }
Write-Host "Generated AppIcon.ico (16-256px) and AppIcon.png (512px)."
