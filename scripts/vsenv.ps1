# Load the MSVC build environment with the Windows SDK that lives on the
# Seagate (registered at E:\Dev\Windows10SDK, currently mounted as H:).
$env:PATH = "C:\Program Files (x86)\Microsoft Visual Studio\Installer;" + $env:PATH
$bat = "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat"
cmd /c "`"$bat`" -arch=amd64 -host_arch=amd64 -no_logo 2>nul && set" | ForEach-Object {
  if ($_ -match '^([^=]+)=(.*)$') { Set-Item -Path "Env:$($matches[1])" -Value $matches[2] }
}
$sdk = 'H:\Dev\Windows10SDK'
if (-not (Test-Path $sdk)) { $sdk = 'E:\Dev\Windows10SDK' }
$v = '10.0.26100.0'
$env:WindowsSdkDir = "$sdk\"
$env:WindowsSDKVersion = "$v\"
$env:INCLUDE = "$sdk\Include\$v\ucrt;$sdk\Include\$v\shared;$sdk\Include\$v\um;$sdk\Include\$v\winrt;$sdk\Include\$v\cppwinrt;" + $env:INCLUDE
$env:LIB = "$sdk\Lib\$v\ucrt\x64;$sdk\Lib\$v\um\x64;" + $env:LIB
$env:PATH = "$sdk\bin\$v\x64;" + $env:PATH
