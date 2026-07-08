# webcamdemo — 跨平台 Webcam 參數控制與即時預覽

webcamdemo 是一個純 Python 的 webcam 工具,同一套程式提供三種用法:當 **library** 匯入(`with Camera() as cam: ...`)、當 **CLI** 操作(`webcamdemo set focus_absolute 120`)、或啟動內建 **web UI**(即時 MJPEG 預覽 + 拉桿即改參數)。Linux 走 V4L2、Windows 走 DirectShow,列舉出相機支援的全部控制項並可即時讀寫。

> **姊妹專案** [visordemo](https://github.com/yazelin/visordemo) 用同一套 `Camera` 介面(context manager + `start_stream`/`read_*`/`set_control`)包 SensoPart VISOR 工業視覺感測器(走 TCP telegram 取像),消費端(如 [qc-station](https://github.com/ching-tech/qc-station))只要換 `camera_factory` 就能在 USB webcam 與工業相機間切換。

## 功能

- **參數全列舉**:列出 v4l2 回報的全部控制項(亮度、對比、對焦、曝光、白平衡...),含範圍、預設值、目前值、選單選項與 inactive(反灰)狀態;另支援 Logitech 相機的隱藏 FOV(視野角)控制。
- **即時 MJPEG 預覽**:內建 HTTP server 串流 `multipart/x-mixed-replace`,瀏覽器直接看畫面,調參數立即看效果。
- **Linux / Windows 雙平台**:Linux 為 V4L2 後端(純 stdlib、零相依),Windows 為 DirectShow 後端。

## 安裝

推薦用 [uv](https://docs.astral.sh/uv/)(新版 Ubuntu/Debian 的系統 pip 受 PEP 668 保護,直接 `pip install` 會報 `externally-managed-environment`):

```bash
git clone https://github.com/yazelin/webcamdemo.git
cd webcamdemo
uv tool install --editable .   # 裝好後直接有 webcamdemo 指令
```

其他方式:

```bash
# Linux 免安裝:本套件在 Linux 零相依,clone 下來直接跑
python3 -m webcamdemo.cli serve

# 傳統 venv
python3 -m venv .venv && .venv/bin/pip install -e .

# Windows(python.org 的 Python 沒有 PEP 668 限制)
pip install -e .
```

- **Linux**:零相依,純 Python stdlib(直接對 `/dev/videoN` 下 ioctl)。
- **Windows**:安裝時自動帶入 `opencv-python` 與 `comtypes`(pyproject 以 environment marker 標注,只在 Windows 裝)。

## 快速開始

啟動 web UI:

```bash
webcamdemo serve
```

然後開瀏覽器到 <http://127.0.0.1:8600> ,左側即時預覽、右側控制項面板。

CLI 範例:

```bash
webcamdemo list                          # 列出所有相機
webcamdemo controls                      # 列出第一台相機的全部控制項
webcamdemo controls -d /dev/video2       # 指定裝置
webcamdemo set brightness 160            # 設定控制項
webcamdemo set power_line_frequency "50 Hz" # 選單型控制項可直接用標籤
webcamdemo get focus_absolute            # 讀取目前值
webcamdemo snapshot -o shot.jpg --size 1920x1080   # 拍一張照片
```

Library 範例:

```python
from webcamdemo import Camera, list_cameras

print(list_cameras())

with Camera() as cam:            # 不給 id 就用第一台相機
    for c in cam.list_controls():
        print(c.id, c.value, c.min, c.max)
    cam.set_control("brightness", 160)
    cam.start_stream(1280, 720, 30)
    jpeg = cam.read_jpeg()       # 一張完整 JPEG frame(bytes)
    open("frame.jpg", "wb").write(jpeg)
    cam.stop_stream()
```

## HTTP API

預設 `127.0.0.1:8600`。`cam` 參數皆可省略,省略即用第一台相機。錯誤回 `{"ok": false, "error": "..."}` 與 4xx/5xx 狀態碼。

| Method | Path | 說明 |
|---|---|---|
| GET | `/` | Web UI(`static/index.html`) |
| GET | `/api/cameras` | JSON `[CameraInfo]`,所有相機 |
| GET | `/api/controls?cam=ID` | JSON `[Control]`,即時值與 inactive 旗標 |
| POST | `/api/control` | body `{"cam":ID,"id":ctrl_id,"value":int}`;回 `{"ok":true,"controls":[...]}` 或 `{"ok":false,"error":str,"controls":[...]}` |
| GET | `/api/formats?cam=ID` | JSON `[FrameFormat]`,支援的解析度/FPS |
| POST | `/api/stream` | body `{"cam":ID,"width":int,"height":int,"fps":float 或 null}`;切換串流格式,回 `{"ok":true}` |
| GET | `/stream.mjpg?cam=ID` | MJPEG 串流(`multipart/x-mixed-replace;boundary=frame`) |
| GET | `/snapshot.jpg?cam=ID` | 單張 JPEG(目前畫面) |

## 已知限制與坑

- **MX Brio 的 FOV 控制**:韌體在 Show Mode 或 RightSight 開啟時會**靜默拒絕** FOV 寫入(不回錯誤,值就是不變)。需要先在 Windows 上用 Logi Options+ 把兩者關閉一次 — 這個狀態存在相機裡,關閉後接回 Linux 即可自由切換 65 / 78 / 90 度。
- **4K 請用 MJPG**:YUYV(未壓縮)在 USB 頻寬下撐不起 4K 影格率,4K 串流請選 MJPG 格式。
- **Windows 後端未實測**:DirectShow 後端是依 Microsoft 文件撰寫,尚未在實機驗證過。若你在 Windows 上使用,請照以下清單驗證並回報 issue:
  1. `pip install -e .` 安裝(確認 opencv-python 與 comtypes 有一起裝上)。
  2. `webcamdemo list` — 應列出至少一台相機。
  3. `webcamdemo controls` — 應列出控制項且範圍/目前值合理。
  4. `webcamdemo serve` 後開 <http://127.0.0.1:8600> — 應看到即時預覽。
  5. `webcamdemo set focus <值>`(或任一可寫控制項;Windows 後端的控制項 id 與 Linux 不同,先用 `webcamdemo controls` 查)— 畫面應有對應變化,`webcamdemo get` 讀回應為設定值。

## 測試

![test](https://github.com/yazelin/webcamdemo/actions/workflows/test.yml/badge.svg)

```bash
python3 -m unittest discover -s tests -v   # 全部測試(無相機也能跑,硬體測試自動 skip)
python3 tests/smoke_test.py                # 實機冒煙測試(無相機時 SKIP,exit 0)
```

測試分四層,前兩層完全不碰硬體:

| 層 | 檔案 | 說明 |
|---|---|---|
| 單元 | `test_model.py`、`test_backend_v4l2_unit.py`、`test_backend_dshow_unit.py` | 純邏輯:資料模型、V4L2 struct ABI/控制項名稱、DirectShow 控制表/GUID/cv2 串流層(以 stub 模擬 COM 與 cv2) |
| 合約 | `test_cli.py`、`test_server_api.py` | CLI 與 HTTP API 行為:以 FakeCamera/FakeBackend 驗證輸出格式、錯誤路徑、擷取失敗(拔線)復原、無預設相機模式 |
| 硬體(自動 skip) | `test_hardware_integration.py` | 唯一會開 `/dev/video*` 的檔案;無相機或裝置被占用(EBUSY)時逐項 skip,改動過的設定一律還原 |
| 冒煙 | `smoke_test.py` | 對實機跑一遍完整流程的獨立腳本,不被 discover 收進上面三層 |

CI(GitHub Actions)在 Linux 與 Windows(皆 Python 3.12;Windows 有 comtypes 無相機)上跑同一條 `unittest discover` 指令。

## 授權

MIT — 林亞澤 Yaze Lin

---

- 原始碼 GitHub:<https://github.com/yazelin/webcamdemo>
- Facebook:<https://www.facebook.com/yaze.lin.gm>
- Buy Me a Coffee:<https://buymeacoffee.com/yazelin>
