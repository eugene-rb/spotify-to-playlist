param([string]$AppPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$NativeApp = if ($AppPath) { (Resolve-Path -LiteralPath $AppPath).Path } else { (Resolve-Path 'native/PlaylistAudioSaver/bin/Release/net10.0-windows/PlaylistAudioSaver.exe').Path }
$TestConfigRoot = Join-Path (Get-Location) 'build/ui-automation'
New-Item -ItemType Directory -Force -Path (Join-Path $TestConfigRoot 'PlaylistAudioSaver') | Out-Null
'{"check_updates":false}' | Set-Content -Path (Join-Path $TestConfigRoot 'PlaylistAudioSaver/config.json') -Encoding ascii
$StartInfo = New-Object System.Diagnostics.ProcessStartInfo
$StartInfo.FileName = $NativeApp
$StartInfo.UseShellExecute = $false
$StartInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
$StartInfo.EnvironmentVariables['APPDATA'] = $TestConfigRoot
$AppProcess = [System.Diagnostics.Process]::Start($StartInfo)
function Find-Id($Root, [string]$Id) {
 $Condition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, $Id)
 return $Root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $Condition)
}
function Wait-Id($Root, [string]$Id) {
 for ($Attempt = 0; $Attempt -lt 100; $Attempt++) {
  $Element = Find-Id $Root $Id
  if ($Element -and $Element.Current.IsEnabled) { return $Element }
  Start-Sleep -Milliseconds 100
 }
 throw "UI element did not become ready: $Id"
}
function Invoke-Button($Element) {
 $Element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
}
try {
 for ($Attempt = 0; $Attempt -lt 100; $Attempt++) {
  $AppProcess.Refresh()
  if ($AppProcess.MainWindowHandle -ne 0) { break }
  Start-Sleep -Milliseconds 100
 }
 $Root = [System.Windows.Automation.AutomationElement]::FromHandle($AppProcess.MainWindowHandle)
 $Load = Wait-Id $Root 'LoadButton'
 (Wait-Id $Root 'SettingsNav').GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
 Wait-Id $Root 'ClientIdInput' | Out-Null
 (Wait-Id $Root 'LibraryNav').GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
 $Url = Find-Id $Root 'UrlInput'
 $Url.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('spotify:track:11dFghVXANMlKmJXsNCbNl')
 Invoke-Button $Load
 $Match = Wait-Id $Root 'MatchButton'
 $Grid = Find-Id $Root 'TrackGrid'
 $GridPattern = $Grid.GetCurrentPattern([System.Windows.Automation.GridPattern]::Pattern)
 if ($GridPattern.Current.RowCount -ne 1) { throw 'Expected one loaded track' }
 $RowCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::DataItem)
 $Row = $Grid.FindFirst([System.Windows.Automation.TreeScope]::Children, $RowCondition)
 $Row.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
 Invoke-Button (Wait-Id $Root 'CorrectButton')
 $Correction = Wait-Id $Root 'CorrectionUrl'
 if (-not (Find-Id $Root 'CorrectionResults')) { throw 'Correction dialog is missing the candidate list' }
 # Wait for the auto-search to settle (the confirm button re-enables once it does).
 Wait-Id $Root 'DialogConfirm' | Out-Null
 $Correction.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('https://example.com/invalid')
 Invoke-Button (Wait-Id $Root 'DialogConfirm')
 Start-Sleep -Milliseconds 500
 Invoke-Button (Wait-Id $Root 'DialogCancel')
 $Load = Wait-Id $Root 'LoadButton'
 if ((Find-Id $Root 'SaveButton').Current.IsEnabled) { throw 'Invalid correction must not enable saving' }
 (Wait-Id $Root 'SettingsNav').GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
 Wait-Id $Root 'ClientIdInput' | Out-Null
 Write-Output 'Native UI automation passed: live track load, row selection, correction dialog, invalid URL recovery, settings navigation'
}
finally {
 $AppProcess.CloseMainWindow() | Out-Null
 if (-not $AppProcess.WaitForExit(3000)) { $AppProcess.Kill() }
}
