@echo off
REM ---------------------------------------------------------------------------
REM replaycam - launcher para Windows.
REM
REM Por que este arquivo existe: o `opencv-python` nao embarca a DLL do
REM OpenH264, entao o FFMPEG tenta abrir o encoder H.264, falha, e o OpenCV cai
REM no encoder nativo do Windows (que funciona). Duas linhas vermelhas
REM "Failed to load OpenH264 library" sao o ruido desse fallback.
REM
REM As variaveis abaixo cortam o log do OpenCV. So funcionam se estiverem no
REM ambiente QUANDO O PROCESSO NASCE - por isso um .cmd e nao Python: definir
REM `os.environ` de dentro do programa nao tem efeito (medido: 8 linhas de ruido
REM tanto antes quanto depois do `import cv2`).
REM
REM Para ver o log nativo do FFmpeg, use `--verbose` (o launcher desliga o
REM silenciamento sozinho) ou `python -m replaycam` direto, sem este launcher.
REM ---------------------------------------------------------------------------
setlocal

REM --verbose e o pedido explicito do log nativo, entao o silenciamento nao pode
REM ser aplicado. Sem isto, `--verbose` junto com o launcher continuava mudo e
REM a opcao mentia. Procura em todos os argumentos, como o proprio argparse faz.
set "REPLAYCAM_VERBOSE="
for %%A in (%*) do (
    if /I "%%~A"=="--verbose" set "REPLAYCAM_VERBOSE=1"
    if /I "%%~A"=="--verbose=true" set "REPLAYCAM_VERBOSE=1"
)

if not defined REPLAYCAM_VERBOSE (
    if not defined OPENCV_FFMPEG_LOGLEVEL set "OPENCV_FFMPEG_LOGLEVEL=QUIET"
    if not defined OPENCV_LOG_LEVEL      set "OPENCV_LOG_LEVEL=SILENT"
)

pushd "%~dp0"
python -m replaycam %*
set "REPLAYCAM_EXIT=%ERRORLEVEL%"
popd

exit /b %REPLAYCAM_EXIT%
