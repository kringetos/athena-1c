//engine: VBScript
//uname: AthenaTextInputBridge
//dname: Athena: мост текстового ввода
//descr: Библиотека минимального диалога. Загрузить перед AthenaTextInput.js.

' The input is passed as arguments, never concatenated into executable script.
' ASCII function/argument names keep compatibility with the VBScript engine.
Function InputText(title, prompt, initialText)
    InputText = InputBox(prompt, title, initialText)
End Function
