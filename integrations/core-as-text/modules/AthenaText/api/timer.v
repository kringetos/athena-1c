:global
:dlls user32.dll
    stdcall int_ptr AthenaSetTimer(HWND, int_ptr, uint, int_ptr)|SetTimer
    stdcall BOOL AthenaKillTimer(HWND, int_ptr)|KillTimer
