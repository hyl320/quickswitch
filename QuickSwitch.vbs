' QuickSwitch silent launcher. Double click to start (no console window).
Option Explicit

Dim fso, shell, base, entry, found, candidates, i
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
entry = base & "\QuickSwitch.pyw"
If Not fso.FileExists(entry) Then
    MsgBox "QuickSwitch.pyw was not found. Please keep this file in the app folder.", 16, "QuickSwitch"
    WScript.Quit 1
End If

' Prefer common install locations, then fall back to PATH.
candidates = Array("D:\py\pythonw.exe", "pythonw.exe")
found = ""
For i = 0 To UBound(candidates)
    If InStr(candidates(i), "\") > 0 Then
        If fso.FileExists(candidates(i)) Then
            found = candidates(i)
            Exit For
        End If
    Else
        found = candidates(i)
        Exit For
    End If
Next

If found = "" Then
    MsgBox "pythonw.exe was not found. Please install Python 3.10 or newer.", 16, "QuickSwitch"
    WScript.Quit 1
End If

shell.CurrentDirectory = base
shell.Run """" & found & """ """ & entry & """", 1, False
