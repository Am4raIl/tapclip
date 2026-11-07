# 🎬 TapClip — Replay Instantâneo de Câmera

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.10 ou superior" />
  <img src="https://img.shields.io/badge/OpenCV-4.14.0-5C3EE8?style=for-the-badge&logo=opencv&logoColor=white" alt="OpenCV 4.14.0" />
  <img src="https://img.shields.io/badge/NumPy-1.26%2B-013243?style=for-the-badge&logo=numpy&logoColor=white" alt="NumPy 1.26 ou superior" />
  <img src="https://img.shields.io/badge/Saída-MP4-111111?style=for-the-badge" alt="Exportação em MP4" />
  <img src="https://img.shields.io/badge/Testes-pytest-0A9EDC?style=for-the-badge&logo=pytest&logoColor=white" alt="Testes com pytest" />
</p>

---

O **TapClip** mantém os últimos segundos de vídeo de uma câmera em um buffer circular na **memória RAM** e exporta um replay em **MP4** quando você aperta **Enter**. A janela padrão é de **30 segundos**, com visualização ao vivo e ajuste do tempo retroativo por um slider.

A captura não grava vídeo continuamente em disco: os arquivos são produzidos quando um replay é solicitado. A aplicação usa **Python, OpenCV e NumPy**, com captura e exportação em threads separadas.

> O nome do projeto é TapClip; o pacote Python, o comando instalado e o launcher Windows ainda se chamam `replaycam`. Os comandos abaixo seguem esses nomes existentes.

## ✨ Funcionalidades

- **Replay retroativo:** buffer circular com descarte dos frames antigos por tempo e limite de capacidade.
- **Gravação por Enter:** disparo pelo console ou pela janela de visualização em foco.
- **Visualização ao vivo:** imagem na resolução entregue pela câmera, com FPS medido, uso de RAM e estado do buffer.
- **Tempo ajustável:** slider para alterar a janela retroativa durante a execução, dentro do orçamento configurado.
- **Captura independente da exportação:** uma thread captura os frames e outra grava os replays.
- **Presets de qualidade:** `--quality high` grava em 1280 × 720 mantendo os 30 segundos (mais RAM e arquivos maiores).
- **Controle de disparos:** debounce e fila com um pedido pendente, substituído quando chega um novo pedido.
- **Exportação validada:** seleção de codec com fallback e reabertura do arquivo para conferir decodificação, contagem de frames e FPS.
- **Arquivos sem sobrescrita:** nomes com data e hora e sufixos quando há colisão.
- **Diagnóstico:** descoberta de câmeras, estatísticas, disparo automático e autoteste sem câmera.

## 🛠️ Stack tecnológica

| Camada | Tecnologias |
| --- | --- |
| Aplicação e CLI | Python 3.10+, `argparse` e `threading` |
| Captura, preview e exportação | OpenCV (`opencv-python==4.14.0.94`) |
| Frames e processamento de imagem | NumPy 1.26+ |
| Buffer e fila | `collections.deque`, locks e `queue.Queue` |
| Empacotamento | setuptools e `pyproject.toml` |
| Testes | pytest |
| Dependências opcionais para extensões | `paho-mqtt` e `pynput` |

## 📁 Estrutura do projeto

```text
tapclip/
├── pyproject.toml             # Metadados, dependências e comando replaycam
├── requirements.txt           # Dependências de execução
├── replaycam.cmd              # Launcher Windows com redução de logs nativos
├── replaycam/
│   ├── __init__.py
│   ├── __main__.py            # Entrada: python -m replaycam
│   ├── cli.py                 # Orquestração, preview, slider e autoteste
│   ├── config.py              # Argumentos e orçamento de memória
│   ├── capture.py             # Captura, sondagem de câmeras e FPS medido
│   ├── ringbuffer.py          # Buffer circular com janela temporal
│   ├── triggers.py            # Gatilho de teclado e referências de extensão
│   └── writer.py              # Exportação MP4 e validação de codecs
├── tests/                     # Testes automatizados
└── replays/                   # Destino padrão dos vídeos gerados
```

## ✅ Pré-requisitos

- **Python 3.10 ou superior**, com `pip` e suporte a ambientes virtuais.
- **Câmera acessível pelo OpenCV**, integrada ou USB, com permissão de acesso no sistema.
- **RAM disponível:** cerca de **791 MiB apenas para os frames** de 30 segundos a 640 × 480 e 30 FPS, além do restante do processo.
- **Ambiente gráfico** para a visualização; use `--no-preview` quando não precisar da janela.

O repositório contém um launcher específico para **Windows** e leitura de teclado para Windows e POSIX. A disponibilidade de câmeras, backends e codecs depende do sistema e do build do OpenCV. A configuração padrão não exige um executável FFmpeg separado no `PATH`.

## 🚀 Rodando pela primeira vez

Execute os comandos na raiz do projeto, na pasta `tapclip`.

### Instalação no Windows — PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Se a política do PowerShell impedir a ativação, use diretamente o Python do ambiente:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m replaycam --self-test
```

O launcher `replaycam.cmd` usa o `python` disponível no `PATH`; ative o ambiente antes de utilizá-lo.

### Instalação em Linux/macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### Validar e iniciar

Com o ambiente virtual ativo, valide primeiro o buffer e a exportação sem acessar a câmera:

```bash
python -m replaycam --self-test
```

O autoteste grava um MP4 de teste e o remove após a validação. Em seguida, descubra o índice da câmera e inicie a aplicação:

```bash
python -m replaycam --list-cameras
python -m replaycam
```

No Windows, prefira o launcher para reduzir as mensagens nativas do OpenCV/FFmpeg:

```powershell
.\replaycam.cmd --list-cameras
.\replaycam.cmd
```

Também é possível instalar o projeto em modo editável e usar o comando `replaycam`:

```bash
python -m pip install -e .
replaycam --help
```

## 🎥 Uso e controles

| Ação | Controle |
| --- | --- |
| Salvar replay | **Enter** no console ou na janela de vídeo em foco |
| Ajustar o tempo retroativo | Arrastar o slider da janela com o mouse |
| Encerrar pela preview | **Esc** |
| Encerrar pelo terminal | **Ctrl+C**; no Windows, **Ctrl+Break** também é tratado |

Espere o buffer cobrir o tempo desejado antes de disparar. Se salvar durante o preenchimento inicial, o replay terá apenas os frames disponíveis. Aumentar o tempo pelo slider também exige esperar por novos frames para preencher a janela maior.

Os vídeos são salvos, por padrão, em:

```text
replays/replay_AAAAMMDD_HHMMSS.mp4
```

Se o nome já existir, o arquivo recebe sufixos como `_1` e `_2`. A resolução do replay é a **resolução do buffer**, enquanto a preview mostra a imagem na resolução da captura.

Por padrão, o buffer é reduzido para 640 × 480. Para gravar em 1280 × 720 sem reduzir os 30 segundos, use `--quality high`. O preset pede 720p à câmera, mantém o buffer no mesmo tamanho e eleva automaticamente o bitrate do H.264. O custo é cerca de 2,3 GiB de RAM e arquivos aproximadamente três vezes maiores que no modo padrão. Se a câmera não entregar 720p, o driver rebaixa e o programa avisa.

### Exemplos de execução

```bash
# Escolher outra câmera
python -m replaycam --device 1

# Manter 10 segundos, sem janela de vídeo
python -m replaycam --seconds 10 --no-preview

# Melhor qualidade: grava em 1280x720 mantendo 30 segundos (~2,3 GiB de RAM)
python -m replaycam --quality high

# Pedir captura em 1080p e manter o buffer em 640x480
python -m replaycam --width 1920 --height 1080 --buf-width 640 --buf-height 480

# Reduzir o consumo de RAM
python -m replaycam --buf-width 320 --buf-height 240 --seconds 30

# Disparar após 10 segundos, com janela de 5 segundos
python -m replaycam --seconds 5 --auto-trigger 10

# Alterar destino, prefixo e codec solicitado
python -m replaycam --outdir meus-replays --prefix clip --fourcc mp4v
```

`--auto-trigger` dispara uma vez após o atraso configurado e **não encerra a aplicação**. O driver pode entregar resolução e FPS diferentes dos solicitados; confira os valores reais no status.

## ⚙️ Principais configurações

| Opção | Padrão | Descrição |
| --- | --- | --- |
| `--device`, `-d` | `0` | Índice da câmera |
| `--backend` | `auto` | Backend: `auto`, `any`, `dshow` ou `msmf`; os dois últimos são específicos do Windows |
| `--width` / `--height` | `0` / `0` | Resolução solicitada ao driver; zero mantém o modo nativo. O preset `high` usa `1280` / `720` |
| `--fps` | `30` | FPS solicitado para captura |
| `--buf-width` / `--buf-height` | `640` / `480` | Dimensões dos frames armazenados em RAM e exportados. O preset `high` usa `1280` / `720` |
| `--quality` | `standard` | Preset de qualidade: `standard` (640 × 480, 1 GiB de orçamento) ou `high` (1280 × 720, 2,5 GiB) |
| `--seconds` | `30` | Janela retroativa solicitada, sujeita ao orçamento do buffer |
| `--max-buffer-mib` | `1024` | Orçamento usado para calcular a capacidade e o teto do slider; o preset `high` usa `2560` |
| `--export-fps` | Igual a `--fps` | FPS do arquivo exportado |
| `--outdir` | `replays` | Diretório de saída |
| `--prefix` | `replay` | Prefixo dos nomes dos vídeos |
| `--fourcc` | Automático | Solicita um codec específico, como `mp4v` |
| `--trigger` | `keyboard` | Gatilho registrado; atualmente apenas teclado |
| `--debounce` | `0.35` | Intervalo mínimo entre disparos, em segundos |
| `--auto-trigger` | `0` | Disparo único após N segundos; zero desativa |
| `--no-preview` | Desativado | Executa sem a janela de vídeo |
| `--stats-interval` | `5` | Intervalo do status no console; zero desativa |
| `--list-cameras` | Desativado | Sonda as câmeras e encerra |
| `--self-test` | Desativado | Valida buffer, concorrência e exportação sem câmera |
| `--verbose` | Desativado | Solicita logs nativos para diagnóstico |

Consulte a lista de argumentos com `python -m replaycam --help`.

## 🧠 Buffer e consumo de memória

Os frames ficam descomprimidos, com três canais BGR. Uma estimativa do consumo é:

```text
RAM dos frames ≈ segundos × FPS × largura × altura × 3 bytes
```

| Resolução do buffer | 5 segundos a 30 FPS | 30 segundos a 30 FPS |
| --- | --- | --- |
| 320 × 240 | 33 MiB | 198 MiB |
| 640 × 480 | 132 MiB | 791 MiB |
| 1280 × 720 | 396 MiB | 2,32 GiB |
| 1920 × 1080 | 890 MiB | 5,21 GiB |

Os valores representam os pixels do buffer. O processo também usa memória para captura, preview, codificação e referências aos frames durante a exportação. **`--max-buffer-mib` não é um limite da memória total do processo.**

O programa limita a janela solicitada conforme o orçamento do buffer. Com os padrões de 1024 MiB e 30 FPS, o teto calculado é de aproximadamente 38 segundos em 640 × 480 e 5,7 segundos em 1920 × 1080. O slider usa posições inteiras e pode oferecer um teto menor.

O preset `--quality high` usa 1280 × 720, 30 FPS e um orçamento de 2560 MiB. Isso comporta os 30 segundos com cerca de 2,3 GiB de frames em RAM. Se o sistema começar a paginar ou a captura perder frames, reduza `--seconds` ou a resolução do buffer.

Para economizar RAM, reduza primeiro a resolução do buffer ou o tempo retroativo. A resolução de captura e a do buffer são independentes: pedir 1080p à câmera não significa exportar o replay em 1080p.

## 🔄 Como funciona

1. A thread de captura lê a câmera, redimensiona os frames para o buffer e os armazena com timestamps.
2. O buffer descarta frames antigos conforme a janela temporal e a capacidade máxima.
3. O Enter enfileira um pedido de replay, respeitando o debounce.
4. A thread de escrita retira o pedido, obtém um snapshot do buffer e exporta o MP4.
5. O arquivo é reaberto para validação e o resultado aparece no console como `SALVO` ou `FALHOU`.

O snapshot copia referências aos frames, sem duplicar todos os pixels. Durante uma exportação, cabe um pedido adicional na fila; novos pedidos substituem esse pedido pendente. O conteúdo de um replay pendente corresponde ao buffer quando a thread de escrita começa a atendê-lo.

A janela temporal evita reter vídeo antigo além do período configurado quando o FPS cai. A exportação, porém, usa FPS fixo: não há reamostragem automática por timestamp. Se a câmera entregar 15 FPS e o arquivo for exportado a 30 FPS, a reprodução ficará acelerada. Confira o FPS medido e ajuste `--export-fps` quando necessário.

## 🔌 Extensões de gatilho

O gatilho disponível é **`keyboard`**. GPIO de Raspberry Pi, MQTT de ESP32, HTTP e hotkey global não estão habilitados como funcionalidades prontas.

O arquivo [replaycam/triggers.py](replaycam/triggers.py) contém o contrato `Trigger` e referências comentadas para extensões. Para adicionar uma fonte, implemente `_read_loop()`, chame `self.fire()` ao detectar o evento e registre a classe em `build_triggers()`, em [replaycam/cli.py](replaycam/cli.py). A gravação permanece na thread de escrita.

Há dependências opcionais para desenvolver integrações com MQTT ou hotkey global:

```bash
python -m pip install -e ".[mqtt]"
python -m pip install -e ".[hotkey]"
```

Instalar esses extras não registra novos gatilhos. Não há servidor HTTP, endpoints ou credenciais de acesso na aplicação atual.

## 🧪 Verificações

Instale as dependências de desenvolvimento e execute a suíte:

```bash
python -m pip install -e ".[dev]"
python -m pytest tests/ -q
```

Os testes cobrem janela temporal, concorrência do buffer, contabilidade de RAM, mudanças de duração, configuração, debounce, sondagem de câmeras, fallback de codec e comportamento da preview e do slider. Os cenários de exportação exigem um codec funcional no ambiente.

Para conferir o caminho de exportação com frames sintéticos, sem câmera:

```bash
python -m replaycam --self-test
```

O código de saída é `0` quando o autoteste passa e `1` quando detecta falhas. Para uma verificação com câmera, execute `python -m replaycam --seconds 5 --auto-trigger 10`, aguarde `SALVO`, abra o MP4 em `replays/` e encerre a aplicação.

## ⚠️ Limitações conhecidas

- **Sem áudio:** o pipeline trabalha apenas com frames de vídeo.
- **Dependência da câmera:** resolução e FPS são pedidos ao driver, sem garantia de atendimento; modo nativo não significa resolução máxima.
- **FPS de exportação fixo:** diferenças em relação à captura real alteram a velocidade e a duração da reprodução.
- **Codec dependente do ambiente:** a cadeia automática tenta `avc1`, `H264`, `X264` e `mp4v`. A tag solicitada pode diferir do codec efetivamente gravado, exibido no log.
- **Sem bitrate explícito:** a CLI não permite escolher bitrate/CRF diretamente. O preset `--quality high` eleva a qualidade e o tamanho do arquivo ao gravar em 1280 × 720; o H.264 do Windows escala o bitrate com a resolução.
- **Foco do teclado:** o Enter precisa chegar ao console ou à preview; não há hotkey global ativa.
- **Slider por mouse:** ajuste o controle por arrasto.
- **Sem limpeza automática:** cada exportação cria um MP4 e os arquivos permanecem no diretório de saída.
- **Sem garantia de salvar todo disparo:** a fila substitui pedidos pendentes para evitar acúmulo de trabalho.

## 🔧 Solução de problemas

| Sintoma | O que verificar |
| --- | --- |
| Nenhuma câmera encontrada | Confira as permissões de câmera, feche outros aplicativos que a estejam usando e execute `--list-cameras`. |
| A câmera desejada não abriu | Teste outro índice encontrado na sondagem; no Windows, compare `--backend dshow` e `--backend msmf`. |
| Enter não salva | Dê foco ao console ou à janela de vídeo e aguarde os primeiros frames. |
| `FALHOU: buffer vazio` | Aguarde o aquecimento da câmera antes de disparar. |
| Replay mais curto que o esperado | Espere o buffer preencher, confira o teto de RAM e compare o FPS medido com `--export-fps`. |
| Vídeo acelerado ou lento | Ajuste `--export-fps` à taxa efetivamente capturada. |
| Gravação com qualidade baixa | Use `--quality high` para gravar em 1280 × 720 e confirme com `--list-cameras --width 1280 --height 720` se a câmera entrega esse modo. |
| Uso elevado de RAM ou perda de frames | Reduza `--buf-width`, `--buf-height` ou `--seconds`; experimente `--no-preview`. Com `--quality high`, reduza `--seconds` primeiro. |
| A preview não abriu | Confira se há sessão gráfica disponível; o programa tenta continuar pelo console quando a criação da janela falha. |
| Mensagens de OpenH264 | No Windows, use `.\replaycam.cmd` para reduzir o ruído. Confira `SALVO` e o codec real; mensagens nativas podem acompanhar um fallback bem-sucedido. |
| Todos os codecs falharam | Teste `--fourcc mp4v` e repita com `--verbose` para obter o diagnóstico nativo. |
| Erro ao criar o diretório de saída | Confira o caminho e as permissões de `--outdir`. |
