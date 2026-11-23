; Uninstall: your data (the local database, settings, connection profiles, logs) stays unless you choose to remove it.
; An update (reinstall over the top) never asks and never removes it. Data in a PostgreSQL server isn't touched either way.
; Installs from before the rename to Recruence kept their data in $APPDATA\MIA Interviewer, which is still used.
!macro customUnInstall
  ${ifNot} ${isUpdated}
    MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also delete your Recruence data on this computer?$\r$\n$\r$\n$APPDATA\Recruence$\r$\n$APPDATA\MIA Interviewer (earlier versions, if present)$\r$\n(local database, saved settings and connections, logs)$\r$\n$\r$\nChoose No to keep it for a later reinstall. Databases on a PostgreSQL server are not affected." /SD IDNO IDNO keepData
      RMDir /r "$APPDATA\Recruence"
      RMDir /r "$APPDATA\MIA Interviewer"
    keepData:
  ${endIf}
!macroend
