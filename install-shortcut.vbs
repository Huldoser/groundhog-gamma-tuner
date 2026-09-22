Option Explicit

Dim fso, shell, root, icoPath, batPath, desktop, linkPath, link

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
icoPath = fso.BuildPath(fso.BuildPath(root, "assets"), "app_icon.ico")
batPath = fso.BuildPath(root, "run.bat")

If Not fso.FileExists(icoPath) Then
  WScript.Echo "Missing icon: " & icoPath
  WScript.Quit 1
End If

If Not fso.FileExists(batPath) Then
  WScript.Echo "Missing launcher: " & batPath
  WScript.Quit 1
End If

desktop = shell.SpecialFolders("Desktop")
If Len(desktop) = 0 Then
  WScript.Echo "Could not find the desktop folder."
  WScript.Quit 1
End If

linkPath = fso.BuildPath(desktop, "Groundhog Gamma Tuner.lnk")
Set link = shell.CreateShortcut(linkPath)
link.TargetPath = batPath
link.WorkingDirectory = root
link.IconLocation = icoPath & ",0"
link.Description = "Groundhog Gamma Tuner"
link.Save

WScript.Echo "Desktop shortcut created: " & linkPath
