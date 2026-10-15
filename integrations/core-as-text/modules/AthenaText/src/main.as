// Minimal modal text input, hosted inside the Designer process by Core.As.
// Windows/VBScript only: no private 1C API, network or configuration writes.
HWND athenaBootstrap;
ASWnd&& athenaBootstrapEvents;
ActiveScript&& athenaTextScript;
AthenaTextSite&& athenaTextSite;

class AthenaTextSite : ScriptSite {
    bool onScriptError(const ScriptError& err) {
        Print("Athena text input error: " + err.description);
        return false;
    }
    void enter() {}
    void leave() {}
}

int main(array<string>&& args) {
    // Wait for the normal UI loop; do not block DLL initialization.
    athenaBootstrap = CreateWindowEx(0, "EDIT".cstr, "".cstr,
        0, 0, 0, 0, 0, 0, 0, 0, 0);
    if (athenaBootstrap == 0) return 1;
    &&athenaBootstrapEvents = attachWndToFunction(athenaBootstrap,
        WndFunc(athenaBootstrapProc), array<uint> = { 0x113 });
    AthenaSetTimer(athenaBootstrap, 1, 750, 0);
    return 0;
}

LRESULT athenaBootstrapProc(uint msg, WPARAM w, LPARAM l) {
    if (msg == 0x113) {
        AthenaKillTimer(athenaBootstrap, 1);
        initActiveScriptSubsystem(0, "Athena text input test");
        &&athenaTextSite = AthenaTextSite();
        &&athenaTextScript = ActiveScript();
        string source = "Dim AthenaEnteredText\nAthenaEnteredText = InputBox(\"Введите текст. Конфигурация не изменяется.\", \"Athena — текстовое поле\", \"\")\n";
        uint prepared = athenaTextScript.prepare("VBScript", source, 0,
            "AthenaTextInput", false, athenaTextSite);
        if (prepared == 0) athenaTextScript.run();
        else Print("Athena VBScript preparation failed: " + prepared);
        return 0;
    }
    return athenaBootstrapEvents.doDefault();
}
