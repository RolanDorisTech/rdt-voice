!macro NSIS_HOOK_POSTINSTALL
  ; Create shortcuts only when the packaged guide and its icon exist.
  IfFileExists "$INSTDIR\resources\windows_installation_guide\windows_installation_guide.html" rdt_guide_html_exists 0
  DetailPrint "WARNING: Installation guide HTML is missing; shortcuts were not created."
  Goto rdt_guide_shortcut_done

  rdt_guide_html_exists:
  IfFileExists "$INSTDIR\resources\windows_installation_guide\rdt_connect_guide.ico" rdt_guide_icon_exists 0
  DetailPrint "WARNING: Installation guide icon is missing; shortcuts were not created."
  Goto rdt_guide_shortcut_done

  rdt_guide_icon_exists:
  CreateDirectory "$SMPROGRAMS\RDT Connect"
  CreateShortCut "$SMPROGRAMS\RDT Connect\RDT Connect Installation Guide.lnk" "$INSTDIR\resources\windows_installation_guide\windows_installation_guide.html" "" "$INSTDIR\resources\windows_installation_guide\rdt_connect_guide.ico"
  CreateShortCut "$DESKTOP\RDT Connect Installation Guide.lnk" "$INSTDIR\resources\windows_installation_guide\windows_installation_guide.html" "" "$INSTDIR\resources\windows_installation_guide\rdt_connect_guide.ico"

  rdt_guide_shortcut_done:
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  Delete "$DESKTOP\RDT Connect Installation Guide.lnk"
  Delete "$SMPROGRAMS\RDT Connect\RDT Connect Installation Guide.lnk"
  RMDir "$SMPROGRAMS\RDT Connect"
!macroend
