//engine: JScript
//uname: AthenaTextInput
//dname: Athena: текстовое поле
//descr: Минимальный макрос ввода текста без изменения конфигурации и обращения к сети.
//addin: global

global.connectGlobals(SelfScript);

var athenaLastText = "";
var athenaMacroName = "Athena: текстовое поле";

function athenaShowTextInput() {
    var bridge = addins.byUniqueName("AthenaTextInputBridge");
    if (!bridge) {
        Message("Athena: сначала загрузите AthenaTextInputBridge.vbs в Снегопат.");
        return;
    }
    var value = bridge.object().InputText(
        "Athena — текстовое поле",
        "Введите текст. Диалог не изменяет код или конфигурацию.",
        athenaLastText
    );
    // InputBox returns an empty string both for Cancel and empty input.
    // Keep the previous nonempty value in that case; no persistence is performed.
    if (value !== null && value !== undefined && String(value).length) {
        athenaLastText = String(value);
    }
}

SelfScript.self["macros" + athenaMacroName] = athenaShowTextInput;

function getDefaultMacros() {
    return athenaMacroName;
}
