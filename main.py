import collections
import configparser
import ctypes
import itertools
import locale
import os
import re
import sys
import tempfile

if sys.platform != 'darwin':
    import notifypy
    # fix: UnsupportedPlatform exception on Windows 11 and Python 3.12 by TransparentLC · Pull Request #55 · ms7m/notify-py
    # https://github.com/ms7m/notify-py/pull/55
    # Temporary fix:
    import functools
    notifypy.Notify._selected_notification_system = functools.partial(notifypy.Notify._selected_notification_system, override_windows_version_detection=True)

import time
import threading
import tkinter as tk
import traceback
import typing
import webbrowser
from PIL import Image
from PIL import ImageTk
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk
from tkinterdnd2 import DND_FILES
from tkinterdnd2 import TkinterDnD

import define
import i18n
import param
import task

# [error] exceeds limit of 178956970 pixels，能否扩大图片像素的限制呢，比如10亿像素。 · Issue #34 · TransparentLC/realesrgan-gui
# https://github.com/TransparentLC/realesrgan-gui/issues/34
# https://github.com/python-pillow/Pillow/blob/e3cca4298011a4e74d6f42b4cfe5a0610d3c79a9/src/PIL/Image.py#L3140
Image.MAX_IMAGE_PIXELS = None

# 深色模式下，滚动条能否统一成深色呢？ · Issue #59 · TransparentLC/realesrgan-gui
# https://github.com/TransparentLC/realesrgan-gui/issues/59
# tk的ScrolledText使用的是tk.Scrollbar，无法应用样式
# 这里从tkinter/scrolledtext.py复制了一份ScrolledText，但是改成了使用ttk.Scrollbar
class ScrolledText(tk.Text):
    def __init__(self, master=None, **kw):
        self.frame = ttk.Frame(master)
        self.vbar = ttk.Scrollbar(self.frame)
        self.vbar.pack(side=tk.RIGHT, fill=tk.Y)

        kw.update({'yscrollcommand': self.vbar.set})
        tk.Text.__init__(self, self.frame, **kw)
        self.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.vbar['command'] = self.yview

        # Copy geometry methods of self.frame without overriding Text
        # methods -- hack!
        text_meths = vars(tk.Text).keys()
        methods = vars(tk.Pack).keys() | vars(tk.Grid).keys() | vars(tk.Place).keys()
        methods = methods.difference(text_meths)

        for m in methods:
            if m[0] != '_' and m != 'config' and m != 'configure':
                setattr(self, m, getattr(self.frame, m))

    def __str__(self):
        return str(self.frame)

class REGUIApp(ttk.Frame):
    def __init__(self, parent: tk.Tk, config: configparser.ConfigParser, models: list[str]):
        super().__init__(parent)
        self.models = models
        for m in (
            'realesrgan-x4plus',
            'realesrgan-x4plus-anime',
        )[::-1]:
            try:
                self.models.insert(0, self.models.pop(self.models.index(m)))
            except ValueError:
                pass
        self.modelFactors: dict[str, int] = {}
        for m in self.models:
            self.modelFactors[m] = 4
            if s := re.search(r'(\d+)x|x(\d+)', m):
                self.modelFactors[m] = int(s.group(1) or s.group(2))

        self.downsample = (
            ('Lanczos', Image.Resampling.LANCZOS),
            ('Bicubic', Image.Resampling.BICUBIC),
            ('Hamming', Image.Resampling.HAMMING),
            ('Bilinear', Image.Resampling.BILINEAR),
            ('Box', Image.Resampling.BOX),
            ('Nearest', Image.Resampling.NEAREST),
        )
        self.tileSize = (0, 32, 64, 128, 256, 512, 1024, 2048, 4096)

        self.config = config

        self.outputPathChanged = True
        self.logPath = os.path.join(define.APP_PATH, 'output.log')
        self.logFile: typing.IO = None
        # 当前的放大进度（0~1）/已放大的文件/总共要放大的文件
        # self.vardoubleProgress.set((self.progressValue[0] + self.progressValue[1]) / self.progressValue[2] * 100)
        self.progressValue: list[int | float] = [0, 0, 1]
        # 初始值/结束值/进度/after ID
        self.progressAnimation: list[float | str] = [0, 0, 0, None]
        # 任务栏进度条
        match sys.platform:
            case 'win32':
                import comtypes.client
                comtypes.client.GetModule(os.path.join(define.BASE_PATH, 'TaskbarLib.tlb'))
                import comtypes.gen.TaskbarLib
                self.progressNativeTaskbar = comtypes.client.CreateObject('{56FDF344-FD6D-11d0-958A-006097C9A090}', interface=comtypes.gen.TaskbarLib.ITaskbarList3)
                self.progressNativeTaskbar.HrInit()
                self.progressNativeTaskbar.ActivateTab(int(self.master.wm_frame(), 16))
                self.progressNativeTaskbar.SetProgressState(int(self.master.wm_frame(), 16), 0) # TBPF_NOPROGRESS
            case _:
                self.progressNativeTaskbar = None
        # 控制是否暂停
        self.pauseEvent = threading.Event()

        self.setupVars()
        self.setupWidgets()

        if self.config['Config'].get('ModelDir'):
            self.writeToOutput(f"Using custom model dir: {self.config['Config'].get('ModelDir')}\n")
        if self.config['Config'].get('Upscaler'):
            self.writeToOutput(f"Using custom upscaler executable: {self.config['Config'].get('Upscaler')}\nThe executable (and models) may be incompatible with Real-ESRGAN-ncnn-vulkan. Use at your own risk!\n")

    def setupVars(self):
        def varstrOutputPathCallback(var: tk.IntVar | tk.StringVar, index: str, mode: str):
            self.outputPathChanged = True
        def outputPathTraceCallback(var: tk.IntVar | tk.StringVar, index: str, mode: str):
            if not self.outputPathChanged:
                self.setInputPath(self.varstrInputPath.get())
        self.varstrInputPath = tk.StringVar()
        self.varstrOutputPath = tk.StringVar()
        self.varstrOutputPath.trace_add('write', varstrOutputPathCallback)
        self.varintResizeMode = tk.IntVar(value=self.config['Config'].getint('ResizeMode'))
        self.varintResizeMode.trace_add('write', outputPathTraceCallback)
        self.varintResizeRatio = tk.IntVar(value=self.config['Config'].getint('ResizeRatio'))
        self.varintResizeRatio.trace_add('write', outputPathTraceCallback)
        self.varintResizeWidth = tk.IntVar(value=self.config['Config'].getint('ResizeWidth'))
        self.varintResizeWidth.trace_add('write', outputPathTraceCallback)
        self.varintResizeHeight = tk.IntVar(value=self.config['Config'].getint('ResizeHeight'))
        self.varintResizeHeight.trace_add('write', outputPathTraceCallback)
        self.varintResizeLongestSide = tk.IntVar(value=self.config['Config'].getint('ResizeLongestSide'))
        self.varintResizeLongestSide.trace_add('write', outputPathTraceCallback)
        self.varintResizeShortestSide = tk.IntVar(value=self.config['Config'].getint('ResizeShortestSide'))
        self.varintResizeShortestSide.trace_add('write', outputPathTraceCallback)
        self.varstrModel = tk.StringVar(value=self.config['Config'].get('Model'))
        self.varstrModel.trace_add('write', outputPathTraceCallback)
        self.varintDownsampleIndex = tk.IntVar(value=self.config['Config'].getint('DownsampleIndex'))
        self.varintTileSizeIndex = tk.IntVar(value=self.config['Config'].getint('TileSizeIndex'))
        self.varintGPUID = tk.IntVar(value=self.config['Config'].getint('GPUID'))
        self.varboolUseTTA = tk.BooleanVar(value=self.config['Config'].getboolean('UseTTA'))
        self.varboolUseWebP = tk.BooleanVar(value=self.config['Config'].getboolean('UseWebP'))
        self.varboolOptimizeGIF = tk.BooleanVar(value=self.config['Config'].getboolean('OptimizeGIF'))
        self.varboolLossyMode = tk.BooleanVar(value=self.config['Config'].getboolean('LossyMode'))
        self.varboolIgnoreError = tk.BooleanVar(value=self.config['Config'].getboolean('IgnoreError'))
        self.varboolPreupscale = tk.BooleanVar(value=self.config['Config'].getboolean('Preupscale'))
        self.varboolProcessing = tk.BooleanVar(value=False)
        self.varboolProcessingPaused = tk.BooleanVar(value=False)
        self.varstrCustomCommand = tk.StringVar(value=self.config['Config'].get('CustomCommand'))
        self.varintLossyQuality = tk.IntVar(value=self.config['Config'].getint('LossyQuality'))
        self.vardoubleProgress = tk.DoubleVar(value=0)

        # StringVars for easily change all labels' strings
        self.varstrLabelInputPath = tk.StringVar(value=i18n.getTranslatedString('Input'))
        self.varstrLabelOutputPath = tk.StringVar(value=i18n.getTranslatedString('Output'))
        self.varstrLabelOpenFileDialogue = tk.StringVar(value=i18n.getTranslatedString('OpenFileDialog'))
        self.varstrLabelUsedModel = tk.StringVar(value=i18n.getTranslatedString('UsedModel'))
        self.varstrLabelResizeMode = tk.StringVar(value=i18n.getTranslatedString('ResizeMode'))
        self.varstrLabelResizeModeRatio = tk.StringVar(value=i18n.getTranslatedString('ResizeModeRatio'))
        self.varstrLabelResizeModeWidth = tk.StringVar(value=i18n.getTranslatedString('ResizeModeWidth'))
        self.varstrLabelResizeModeHeight = tk.StringVar(value=i18n.getTranslatedString('ResizeModeHeight'))
        self.varstrLabelResizeModeLongestSide = tk.StringVar(value=i18n.getTranslatedString('ResizeModeLongestSide'))
        self.varstrLabelResizeModeShortestSide = tk.StringVar(value=i18n.getTranslatedString('ResizeModeShortestSide'))
        self.varstrLabelStartProcessing = tk.StringVar(value=i18n.getTranslatedString(('ContinueProcessing' if self.varboolProcessingPaused.get() else 'PauseProcessing') if self.varboolProcessing.get() else 'StartProcessing'))
        self.varstrLabelDownsampleMode = tk.StringVar(value=i18n.getTranslatedString('DownsampleMode'))
        self.varstrLabelTileSize = tk.StringVar(value=i18n.getTranslatedString('TileSize'))
        self.varstrLabelTileSizeAuto = tk.StringVar(value=i18n.getTranslatedString('TileSizeAuto'))
        self.varstrLabelUsedGPUID = tk.StringVar(value=i18n.getTranslatedString('UsedGPUID'))
        self.varstrLabelLossyModeQuality = tk.StringVar(value=i18n.getTranslatedString('LossyModeQuality'))
        self.varstrLabelCustomCommand = tk.StringVar(value=i18n.getTranslatedString('CustomCommand'))
        self.varstrLabelPreferWebP = tk.StringVar(value=i18n.getTranslatedString('PreferWebP'))
        self.varstrLabelEnableTTA = tk.StringVar(value=i18n.getTranslatedString('EnableTTA'))
        self.varstrLabelGIFOptimizeTransparency = tk.StringVar(value=i18n.getTranslatedString('GIFOptimizeTransparency'))
        self.varstrLabelEnableLossyMode = tk.StringVar(value=i18n.getTranslatedString('EnableLossyMode'))
        self.varstrLabelEnableIgnoreError = tk.StringVar(value=i18n.getTranslatedString('EnableIgnoreError'))
        self.varstrLabelEnablePreupscale = tk.StringVar(value=i18n.getTranslatedString('EnablePreupscale'))
        self.varstrLabelViewREGUISource = tk.StringVar(value=i18n.getTranslatedString('ViewREGUISource'))
        self.varstrLabelViewRESource = tk.StringVar(value=i18n.getTranslatedString('ViewRESource'))
        self.varstrLabelViewAdditionalModel = tk.StringVar(value=i18n.getTranslatedString('ViewAdditionalModel'))
        self.varstrLabelViewDonatePage = tk.StringVar(value=i18n.getTranslatedString('ViewDonatePage'))
        self.varstrLabelFrameBasicConfig = tk.StringVar(value=i18n.getTranslatedString('FrameBasicConfig'))

    def setupWidgets(self):
        self.rowconfigure(0, weight=0)
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self.notebookConfig = ttk.Notebook(self)
        self.notebookConfig.grid(row=0, column=0, padx=5, pady=5, sticky=tk.NSEW)

        self.frameBasicConfig = ttk.Frame(self.notebookConfig, padding=5)
        self.frameBasicConfig.grid(row=0, column=0, padx=5, pady=5, sticky=tk.NSEW)
        ttk.Label(self.frameBasicConfig, textvariable=self.varstrLabelInputPath).pack(padx=10, pady=5, fill=tk.X)
        self.frameInputPath = ttk.Frame(self.frameBasicConfig)
        self.frameInputPath.columnconfigure(0, weight=1)
        self.frameInputPath.columnconfigure(1, weight=0)
        self.frameInputPath.pack(padx=5, pady=5, fill=tk.X)
        self.entryInputPath = ttk.Entry(self.frameInputPath, textvariable=self.varstrInputPath)
        self.entryInputPath.grid(row=0, column=0, padx=5, sticky=tk.EW)
        self.buttonInputPath = ttk.Button(self.frameInputPath, textvariable=self.varstrLabelOpenFileDialogue, command=self.buttonInputPath_click)
        self.buttonInputPath.grid(row=0, column=1, padx=5)
        ttk.Label(self.frameBasicConfig, textvariable=self.varstrLabelOutputPath).pack(padx=10, pady=5, fill=tk.X)
        self.frameOutputPath = ttk.Frame(self.frameBasicConfig)
        self.frameOutputPath.columnconfigure(0, weight=1)
        self.frameOutputPath.columnconfigure(1, weight=0)
        self.frameOutputPath.pack(padx=5, pady=5, fill=tk.X)
        self.entryOutputPath = ttk.Entry(self.frameOutputPath, textvariable=self.varstrOutputPath)
        self.entryOutputPath.grid(row=0, column=0, padx=5, sticky=tk.EW)
        self.buttonOutputPath = ttk.Button(self.frameOutputPath, textvariable=self.varstrLabelOpenFileDialogue, command=self.buttonOutputPath_click)
        self.buttonOutputPath.grid(row=0, column=1, padx=5)
        self.frameBasicConfigBottom = ttk.Frame(self.frameBasicConfig)
        self.frameBasicConfigBottom.columnconfigure(0, weight=0)
        self.frameBasicConfigBottom.columnconfigure(1, weight=1)
        self.frameBasicConfigBottom.pack(fill=tk.X)
        self.frameModel = ttk.Frame(self.frameBasicConfigBottom)
        self.frameModel.grid(row=0, column=1, sticky=tk.NSEW)
        ttk.Label(self.frameModel, textvariable=self.varstrLabelUsedModel).pack(padx=10, pady=5, fill=tk.X)
        self.comboModel = ttk.Combobox(self.frameModel, state='readonly', values=self.models, textvariable=self.varstrModel)
        if self.varstrModel.get() in self.models:
            self.comboModel.current(self.models.index(self.varstrModel.get()))
        else:
            self.varstrModel.set(self.models[0])
        self.comboModel.pack(padx=10, pady=5, fill=tk.X)
        self.comboModel.bind('<<ComboboxSelected>>', lambda e: e.widget.select_clear())
        self.frameResize = ttk.Frame(self.frameBasicConfigBottom)
        self.frameResize.grid(row=0, column=0, sticky=tk.NSEW)
        ttk.Label(self.frameResize, textvariable=self.varstrLabelResizeMode).grid(row=0, column=0, columnspan=2, padx=10, pady=5, sticky=tk.EW)
        self.radioResizeRatio = ttk.Radiobutton(self.frameResize, textvariable=self.varstrLabelResizeModeRatio, value=int(param.ResizeMode.RATIO), variable=self.varintResizeMode)
        self.radioResizeRatio.grid(row=1, column=0, padx=5, pady=5, sticky=tk.EW)
        self.spinResizeRatio = ttk.Spinbox(self.frameResize, from_=2, to=16, increment=1, width=12, textvariable=self.varintResizeRatio)
        self.spinResizeRatio.grid(row=1, column=1, padx=5, pady=5, sticky=tk.EW)
        self.radioResizeWidth = ttk.Radiobutton(self.frameResize, textvariable=self.varstrLabelResizeModeWidth, value=int(param.ResizeMode.WIDTH), variable=self.varintResizeMode)
        self.radioResizeWidth.grid(row=2, column=0, padx=5, pady=5, sticky=tk.EW)
        self.spinResizeWidth = ttk.Spinbox(self.frameResize, from_=1, to=16383, increment=1, width=12, textvariable=self.varintResizeWidth)
        self.spinResizeWidth.grid(row=2, column=1, padx=5, pady=5, sticky=tk.EW)
        self.radioResizeHeight = ttk.Radiobutton(self.frameResize, textvariable=self.varstrLabelResizeModeHeight, value=int(param.ResizeMode.HEIGHT), variable=self.varintResizeMode)
        self.radioResizeHeight.grid(row=3, column=0, padx=5, pady=5, sticky=tk.EW)
        self.spinResizeHeight = ttk.Spinbox(self.frameResize, from_=1, to=16383, increment=1, width=12, textvariable=self.varintResizeHeight)
        self.spinResizeHeight.grid(row=3, column=1, padx=5, pady=5, sticky=tk.EW)
        self.radioResizeLongestSide = ttk.Radiobutton(self.frameResize, textvariable=self.varstrLabelResizeModeLongestSide, value=int(param.ResizeMode.LONGEST_SIDE), variable=self.varintResizeMode)
        self.radioResizeLongestSide.grid(row=4, column=0, padx=5, pady=5, sticky=tk.EW)
        self.spinResizeLongestSide = ttk.Spinbox(self.frameResize, from_=1, to=16383, increment=1, width=12, textvariable=self.varintResizeLongestSide)
        self.spinResizeLongestSide.grid(row=4, column=1, padx=5, pady=5, sticky=tk.EW)
        self.radioResizeShortestSide = ttk.Radiobutton(self.frameResize, textvariable=self.varstrLabelResizeModeShortestSide, value=int(param.ResizeMode.SHORTEST_SIDE), variable=self.varintResizeMode)
        self.radioResizeShortestSide.grid(row=5, column=0, padx=5, pady=5, sticky=tk.EW)
        self.spinResizeShortestSide = ttk.Spinbox(self.frameResize, from_=1, to=16383, increment=1, width=12, textvariable=self.varintResizeShortestSide)
        self.spinResizeShortestSide.grid(row=5, column=1, padx=5, pady=5, sticky=tk.EW)
        self.buttonProcess = ttk.Button(self.frameBasicConfigBottom, textvariable=self.varstrLabelStartProcessing, style='Accent.TButton', width=6, command=self.buttonProcess_click)
        self.buttonProcess.grid(row=0, column=1, padx=5, pady=5, sticky=tk.SE)

        self.frameAdvancedConfig = ttk.Frame(self.notebookConfig, padding=5)
        self.frameAdvancedConfig.grid(row=0, column=0, padx=5, pady=5, sticky=tk.NSEW)
        self.frameAdvancedConfig.columnconfigure(0, weight=1)
        self.frameAdvancedConfig.columnconfigure(1, weight=3)
        self.frameAdvancedConfigLeft = ttk.Frame(self.frameAdvancedConfig)
        self.frameAdvancedConfigLeft.grid(row=0, column=0, sticky=tk.NSEW)
        self.frameAdvancedConfigRight = ttk.Frame(self.frameAdvancedConfig)
        self.frameAdvancedConfigRight.grid(row=0, column=1, sticky=tk.NSEW)
        self.frameAdvancedConfigLeftSub = ttk.Frame(self.frameAdvancedConfigLeft)
        self.frameAdvancedConfigLeftSub.pack(fill=tk.X)
        self.frameAdvancedConfigLeftSub.columnconfigure(0, weight=1)
        self.frameAdvancedConfigLeftSub.columnconfigure(1, weight=1)
        self.frameAdvancedConfigLeftSubLeft = ttk.Frame(self.frameAdvancedConfigLeftSub)
        self.frameAdvancedConfigLeftSubLeft.grid(row=0, column=0, sticky=tk.NSEW)
        self.frameAdvancedConfigLeftSubRight = ttk.Frame(self.frameAdvancedConfigLeftSub)
        self.frameAdvancedConfigLeftSubRight.grid(row=0, column=1, sticky=tk.NSEW)
        ttk.Label(self.frameAdvancedConfigLeftSubLeft, textvariable=self.varstrLabelDownsampleMode).pack(padx=10, pady=5, fill=tk.X)
        self.comboDownsample = ttk.Combobox(self.frameAdvancedConfigLeftSubLeft, state='readonly', values=tuple(x[0] for x in self.downsample), width=12)
        self.comboDownsample.current(self.varintDownsampleIndex.get())
        self.comboDownsample.pack(padx=10, pady=5, fill=tk.X)
        self.comboDownsample.bind('<<ComboboxSelected>>', self.comboDownsample_click)
        ttk.Label(self.frameAdvancedConfigLeftSubRight, textvariable=self.varstrLabelTileSize).pack(padx=10, pady=5, fill=tk.X)
        self.comboTileSize = ttk.Combobox(self.frameAdvancedConfigLeftSubRight, state='readonly', values=(self.varstrLabelTileSizeAuto.get(), *self.tileSize[1:]), width=12)
        self.comboTileSize.current(self.varintTileSizeIndex.get())
        self.comboTileSize.pack(padx=10, pady=5, fill=tk.X)
        ttk.Label(self.frameAdvancedConfigLeft, textvariable=self.varstrLabelUsedGPUID).pack(padx=10, pady=5, fill=tk.X)
        self.spinGPUID = ttk.Spinbox(self.frameAdvancedConfigLeft, from_=-1, to=7, increment=1, width=12, textvariable=self.varintGPUID)
        self.spinGPUID.pack(padx=10, pady=5, fill=tk.X)
        ttk.Label(self.frameAdvancedConfigLeft, textvariable=self.varstrLabelLossyModeQuality).pack(padx=10, pady=5, fill=tk.X)
        self.spinLossyQuality = ttk.Spinbox(self.frameAdvancedConfigLeft, from_=0, to=100, increment=5, width=12, textvariable=self.varintLossyQuality)
        self.spinLossyQuality.set(self.varintLossyQuality.get())
        self.spinLossyQuality.pack(padx=10, pady=5, fill=tk.X)
        self.comboTileSize.bind('<<ComboboxSelected>>', self.comboTileSize_click)
        ttk.Label(self.frameAdvancedConfigLeft, textvariable=self.varstrLabelCustomCommand).pack(padx=10, pady=5, fill=tk.X)
        self.entryCustomCommand = ttk.Entry(self.frameAdvancedConfigLeft, textvariable=self.varstrCustomCommand)
        self.entryCustomCommand.pack(padx=10, pady=5, fill=tk.X)
        self.checkUseWebP = ttk.Checkbutton(self.frameAdvancedConfigRight, textvariable=self.varstrLabelPreferWebP, style='Switch.TCheckbutton', variable=self.varboolUseWebP)
        self.checkUseWebP.pack(padx=10, pady=5, fill=tk.X)
        self.checkUseTTA = ttk.Checkbutton(self.frameAdvancedConfigRight, textvariable=self.varstrLabelEnableTTA, style='Switch.TCheckbutton', variable=self.varboolUseTTA)
        self.checkUseTTA.pack(padx=10, pady=5, fill=tk.X)
        self.checkOptimizeGIF = ttk.Checkbutton(self.frameAdvancedConfigRight, textvariable=self.varstrLabelGIFOptimizeTransparency, style='Switch.TCheckbutton', variable=self.varboolOptimizeGIF)
        self.checkOptimizeGIF.pack(padx=10, pady=5, fill=tk.X)
        self.checkLossyMode = ttk.Checkbutton(self.frameAdvancedConfigRight, textvariable=self.varstrLabelEnableLossyMode, style='Switch.TCheckbutton', variable=self.varboolLossyMode)
        self.checkLossyMode.pack(padx=10, pady=5, fill=tk.X)
        self.checkIgnoreError = ttk.Checkbutton(self.frameAdvancedConfigRight, textvariable=self.varstrLabelEnableIgnoreError, style='Switch.TCheckbutton', variable=self.varboolIgnoreError)
        self.checkIgnoreError.pack(padx=10, pady=5, fill=tk.X)
        self.checkPreupscale = ttk.Checkbutton(self.frameAdvancedConfigRight, textvariable=self.varstrLabelEnablePreupscale, style='Switch.TCheckbutton', variable=self.varboolPreupscale)
        self.checkPreupscale.pack(padx=10, pady=5, fill=tk.X)
        self.comboLanguage = ttk.Combobox(self.frameAdvancedConfigRight, state='readonly', values=tuple(i18n.locales_map.keys()))
        self.comboLanguage.current(i18n.get_current_locale_display_name())
        self.comboLanguage.pack(padx=10, pady=5, fill=tk.X)
        self.comboLanguage.bind('<<ComboboxSelected>>', self.change_app_lang)

        self.frameAbout = ttk.Frame(self.notebookConfig, padding=5)
        self.frameAbout.grid(row=0, column=0, padx=5, pady=5, sticky=tk.NSEW)
        self.frameAboutContent = ttk.Frame(self.frameAbout)
        self.frameAboutContent.place(relx=.5, rely=.5, anchor=tk.CENTER)
        f = ttk.Label().cget('font').string.split(' ')
        f[-1] = '16'
        f = ' '.join(f)
        self.imageIcon = ImageTk.PhotoImage(Image.open(os.path.join(define.BASE_PATH, 'icon-128px.png')))
        ttk.Label(self.frameAboutContent, image=self.imageIcon).pack(padx=10, pady=10)
        ttk.Label(self.frameAboutContent, text=define.APP_TITLE, font=f, justify=tk.CENTER).pack()
        ttk.Label(self.frameAboutContent, text='By TransparentLC' + (time.strftime("\nBuilt at %Y-%m-%d %H:%M:%S", time.localtime(define.BUILD_TIME)) if define.BUILD_TIME else ""), justify=tk.CENTER).pack()
        self.frameAboutBottom = ttk.Frame(self.frameAboutContent)
        self.frameAboutBottom.pack()
        ttk.Button(self.frameAboutBottom, textvariable=self.varstrLabelViewREGUISource, command=lambda: webbrowser.open_new_tab('https://github.com/TransparentLC/realesrgan-gui')).grid(row=0, column=0, padx=5, pady=5, sticky=tk.NSEW)
        ttk.Button(self.frameAboutBottom, textvariable=self.varstrLabelViewRESource, command=lambda: webbrowser.open_new_tab('https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan')).grid(row=0, column=1, padx=5, pady=5, sticky=tk.NSEW)
        ttk.Button(self.frameAboutBottom, textvariable=self.varstrLabelViewAdditionalModel, command=lambda: webbrowser.open_new_tab('https://github.com/TransparentLC/realesrgan-gui/releases/tag/additional-models')).grid(row=1, column=0, padx=5, pady=5, sticky=tk.NSEW)
        ttk.Button(self.frameAboutBottom, textvariable=self.varstrLabelViewDonatePage, command=lambda: webbrowser.open_new_tab('https://i.akarin.dev/donate/')).grid(row=1, column=1, padx=5, pady=5, sticky=tk.NSEW)

        self.notebookConfig.add(self.frameBasicConfig, text=i18n.getTranslatedString('FrameBasicConfig'))
        self.notebookConfig.add(self.frameAdvancedConfig, text=i18n.getTranslatedString('FrameAdvancedConfig'))
        self.notebookConfig.add(self.frameAbout, text=i18n.getTranslatedString('FrameAbout'))

        self.textOutput = ScrolledText(self)
        self.textOutput.grid(row=1, column=0, padx=5, pady=5, sticky=tk.NSEW)
        self.textOutput.configure(state=tk.DISABLED)

        self.progressbar = ttk.Progressbar(self, orient='horizontal', mode='determinate', variable=self.vardoubleProgress)
        self.progressbar.grid(row=2, column=0, padx=5, pady=5, sticky=tk.NSEW)

    def change_app_lang(self, event: tk.Event):
        lang = self.comboLanguage.get()
        lang = i18n.locales_map[lang]
        i18n.set_current_language(lang)

        self.notebookConfig.tab(self.frameBasicConfig, text=i18n.getTranslatedString('FrameBasicConfig'))
        self.notebookConfig.tab(self.frameAdvancedConfig, text=i18n.getTranslatedString('FrameAdvancedConfig'))
        self.notebookConfig.tab(self.frameAbout, text=i18n.getTranslatedString('FrameAbout'))

        self.varstrLabelInputPath.set(i18n.getTranslatedString('Input'))
        self.varstrLabelOutputPath.set(i18n.getTranslatedString('Output'))
        self.varstrLabelOpenFileDialogue.set(i18n.getTranslatedString('OpenFileDialog'))
        self.varstrLabelUsedModel.set(i18n.getTranslatedString('UsedModel'))
        self.varstrLabelResizeMode.set(i18n.getTranslatedString('ResizeMode'))
        self.varstrLabelResizeModeRatio.set(i18n.getTranslatedString('ResizeModeRatio'))
        self.varstrLabelResizeModeWidth.set(i18n.getTranslatedString('ResizeModeWidth'))
        self.varstrLabelResizeModeHeight.set(i18n.getTranslatedString('ResizeModeHeight'))
        self.varstrLabelResizeModeLongestSide.set(i18n.getTranslatedString('ResizeModeLongestSide'))
        self.varstrLabelResizeModeShortestSide.set(i18n.getTranslatedString('ResizeModeShortestSide'))
        self.varstrLabelStartProcessing.set(i18n.getTranslatedString(('ContinueProcessing' if self.varboolProcessingPaused.get() else 'PauseProcessing') if self.varboolProcessing.get() else 'StartProcessing'))
        self.varstrLabelDownsampleMode.set(i18n.getTranslatedString('DownsampleMode'))

        self.varstrLabelTileSize.set(i18n.getTranslatedString('TileSize'))
        self.varstrLabelTileSizeAuto.set(i18n.getTranslatedString('TileSizeAuto'))
        self.comboTileSize['values'] = (self.varstrLabelTileSizeAuto.get(), *self.tileSize[1:])
        self.comboTileSize.current(self.varintTileSizeIndex.get())

        self.varstrLabelUsedGPUID.set(i18n.getTranslatedString('UsedGPUID'))
        self.varstrLabelLossyModeQuality.set(i18n.getTranslatedString('LossyModeQuality'))
        self.varstrLabelCustomCommand.set(i18n.getTranslatedString('CustomCommand'))
        self.varstrLabelPreferWebP.set(i18n.getTranslatedString('PreferWebP'))
        self.varstrLabelEnableTTA.set(value=i18n.getTranslatedString('EnableTTA'))
        self.varstrLabelGIFOptimizeTransparency.set(i18n.getTranslatedString('GIFOptimizeTransparency'))
        self.varstrLabelEnableLossyMode.set(i18n.getTranslatedString('EnableLossyMode'))
        self.varstrLabelEnableIgnoreError.set(i18n.getTranslatedString('EnableIgnoreError'))
        self.varstrLabelEnablePreupscale.set(i18n.getTranslatedString('EnablePreupscale'))
        self.varstrLabelViewREGUISource.set(i18n.getTranslatedString('ViewREGUISource'))
        self.varstrLabelViewRESource.set(i18n.getTranslatedString('ViewRESource'))
        self.varstrLabelViewAdditionalModel.set(i18n.getTranslatedString('ViewAdditionalModel'))
        self.varstrLabelViewDonatePage.set(i18n.getTranslatedString('ViewDonatePage'))
        self.varstrLabelFrameBasicConfig.set(i18n.getTranslatedString('FrameBasicConfig'))

    def close(self):
        self.config['DEFAULT'] = {}
        self.config['Config'] = {
            'Upscaler': self.config['Config'].get('Upscaler') or '',
            'ModelDir': self.config['Config'].get('ModelDir') or '',
            'ResizeMode': self.varintResizeMode.get(),
            'ResizeRatio': self.varintResizeRatio.get(),
            'ResizeWidth': self.varintResizeWidth.get(),
            'ResizeHeight': self.varintResizeHeight.get(),
            'ResizeLongestSide': self.varintResizeLongestSide.get(),
            'ResizeShortestSide': self.varintResizeShortestSide.get(),
            'Model': self.varstrModel.get(),
            'DownsampleIndex': self.varintDownsampleIndex.get(),
            'GPUID': self.varintGPUID.get(),
            'TileSizeIndex': self.varintTileSizeIndex.get(),
            'LossyQuality': self.varintLossyQuality.get(),
            'UseWebP': self.varboolUseWebP.get(),
            'UseTTA': self.varboolUseTTA.get(),
            'OptimizeGIF': self.varboolOptimizeGIF.get(),
            'LossyMode': self.varboolLossyMode.get(),
            'IgnoreError': self.varboolIgnoreError.get(),
            'Preupscale': self.varboolPreupscale.get(),
            'CustomCommand': self.varstrCustomCommand.get(),
            'AppLanguage': i18n.current_language
        }
        with open(define.APP_CONFIG_PATH, 'w', encoding='utf-8') as f:
            self.config.write(f)

    def buttonInputPath_click(self):
        p = filedialog.askopenfilename(filetypes=(
            ('Image files', ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.tif', '.tiff')),
        ))
        if not p:
            return
        self.setInputPath(p)

    def buttonOutputPath_click(self):
        p = filedialog.askopenfilename(filetypes=(
            ('Image files', ('.png', '.gif', '.webp')),
        ))
        if not p:
            return
        self.varstrOutputPath.set(p)

    def comboDownsample_click(self, event: tk.Event):
        self.comboDownsample.select_clear()
        self.varintDownsampleIndex.set(self.comboDownsample.current())

    def comboTileSize_click(self, event: tk.Event):
        self.comboTileSize.select_clear()
        self.varintTileSizeIndex.set(self.comboTileSize.current())

    def buttonProcess_click(self):
        if self.varboolProcessing.get():
            if self.varboolProcessingPaused.get():
                self.varboolProcessingPaused.set(False)
                self.pauseEvent.set()
            else:
                self.varboolProcessingPaused.set(True)
                self.pauseEvent.clear()
                self.writeToOutput('Will pause after current task is completed.\n')
            self.buttonProcess.config(style='' if self.varboolProcessing.get() and not self.varboolProcessingPaused.get() else 'Accent.TButton')
            self.varstrLabelStartProcessing.set(i18n.getTranslatedString(('ContinueProcessing' if self.varboolProcessingPaused.get() else 'PauseProcessing') if self.varboolProcessing.get() else 'StartProcessing'))
            return
        try:
            inputPath = self.varstrInputPath.get()
            outputPath = self.varstrOutputPath.get()
            if not inputPath or not outputPath:
                return messagebox.showwarning(define.APP_TITLE, i18n.getTranslatedString('WarningInvalidPath'))
            inputPath = os.path.normpath(inputPath)
            outputPath = os.path.normpath(outputPath)
            if not os.path.exists(inputPath):
                return messagebox.showwarning(define.APP_TITLE, i18n.getTranslatedString('WarningNotFoundPath'))

            initialConfigParams = self.getConfigParams()
            if initialConfigParams.resizeMode == param.ResizeMode.RATIO and initialConfigParams.resizeModeValue == 1:
                return messagebox.showwarning(define.APP_TITLE, i18n.getTranslatedString('WarningResizeRatio'))

            self.vardoubleProgress.set(0)
            self.progressAnimation[0] = 0
            self.progressAnimation[1] = 0
            self.progressAnimation[2] = 0
            if self.progressAnimation[3]:
                self.progressbar.after_cancel(self.progressAnimation[3])
                self.progressAnimation[3] = None

            queue = collections.deque()
            if os.path.isdir(inputPath):
                self.progressValue[0] = 0
                self.progressValue[1] = 0
                self.progressValue[2] = 0
                for curDir, dirs, files in os.walk(inputPath):
                    for f in files:
                        if os.path.splitext(f)[1].lower() not in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.tif', '.tiff'}:
                            continue
                        f = os.path.join(curDir, f)
                        g = os.path.join(outputPath, f.removeprefix(inputPath + os.path.sep))
                        if os.path.splitext(f)[1].lower() == '.gif':
                            queue.append(task.SplitGIFTask(self.writeToOutput, self.progressValue, f, g, initialConfigParams, queue, self.varboolOptimizeGIF.get()))
                        elif self.varstrCustomCommand.get().strip():
                            t = tempfile.mktemp('.png')
                            queue.append(task.RESpawnTask(self.writeToOutput, self.progressValue, f, t, initialConfigParams))
                            queue.append(task.CustomCompressTask(self.writeToOutput, t, g, self.varstrCustomCommand.get().strip(), True))
                        elif self.varboolLossyMode.get() and os.path.splitext(g)[1].lower() in {'.jpg', '.jpeg', '.webp'}:
                            t = tempfile.mktemp('.webp')
                            queue.append(task.RESpawnTask(self.writeToOutput, self.progressValue, f, t, initialConfigParams))
                            queue.append(task.LossyCompressTask(self.writeToOutput, t, g, self.varintLossyQuality.get(), True))
                        else:
                            if os.path.splitext(f)[1].lower() in {'.tif', '.tiff'}:
                                g = os.path.splitext(g)[0] + ('.webp' if self.varboolUseWebP.get() else '.png')
                            queue.append(task.RESpawnTask(self.writeToOutput, self.progressValue, f, g, initialConfigParams))
                        self.progressValue[2] += 1
                if not queue:
                    return messagebox.showwarning(define.APP_TITLE, i18n.getTranslatedString('WarningEmptyFolder'))
            elif os.path.splitext(inputPath)[1].lower() in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.tif', '.tiff'}:
                self.progressValue[0] = 0
                self.progressValue[1] = 0
                self.progressValue[2] = 1
                if os.path.splitext(inputPath)[1].lower() == '.gif':
                    queue.append(task.SplitGIFTask(self.writeToOutput, self.progressValue, inputPath, outputPath, initialConfigParams, queue, self.varboolOptimizeGIF.get()))
                elif self.varstrCustomCommand.get().strip():
                    t = tempfile.mktemp('.png')
                    queue.append(task.RESpawnTask(self.writeToOutput, self.progressValue, inputPath, t, initialConfigParams))
                    queue.append(task.CustomCompressTask(self.writeToOutput, t, outputPath, self.varstrCustomCommand.get().strip(), True))
                elif self.varboolLossyMode.get() and os.path.splitext(outputPath)[1].lower() in {'.jpg', '.jpeg', '.webp'}:
                    t = tempfile.mktemp('.webp')
                    queue.append(task.RESpawnTask(self.writeToOutput, self.progressValue, inputPath, t, initialConfigParams))
                    queue.append(task.LossyCompressTask(self.writeToOutput, t, outputPath, self.varintLossyQuality.get(), True))
                else:
                    queue.append(task.RESpawnTask(self.writeToOutput, self.progressValue, inputPath, outputPath, initialConfigParams))
            else:
                return messagebox.showwarning(define.APP_TITLE, i18n.getTranslatedString('WarningInvalidFormat'))
            self.varboolProcessing.set(True)
            self.varboolProcessingPaused.set(False)
            self.pauseEvent.set()
            self.buttonProcess.config(style='' if self.varboolProcessing.get() and not self.varboolProcessingPaused.get() else 'Accent.TButton')
            self.varstrLabelStartProcessing.set(i18n.getTranslatedString(('ContinueProcessing' if self.varboolProcessingPaused.get() else 'PauseProcessing') if self.varboolProcessing.get() else 'StartProcessing'))
            self.textOutput.config(state=tk.NORMAL)
            self.textOutput.delete(1.0, tk.END)
            self.textOutput.config(state=tk.DISABLED)

            if sys.platform != 'darwin':
                notification = notifypy.Notify(
                    default_notification_application_name=define.APP_TITLE,
                    default_notification_icon=os.path.join(define.BASE_PATH, 'icon-128px.png'),
                )
            match sys.platform:
                case 'win32':
                    self.progressNativeTaskbar.SetProgressState(int(self.master.wm_frame(), 16), 2) # TBPF_NORMAL
                    # 初始进度应该是0，但是直接设为0没有效果，所以改成使用非常接近0的值
                    self.progressNativeTaskbar.SetProgressValue(int(self.master.wm_frame(), 16), 1, 0xFFFFFFFF)
            ts = time.perf_counter()
            def completeCallback(withError: bool):
                te = time.perf_counter()
                if sys.platform != 'darwin':
                    notification.title = i18n.getTranslatedString('ToastCompletedTitle')
                    if withError:
                        notification.message = i18n.getTranslatedString('ToastCompletedMessageWithError').format(self.logPath)
                    else:
                        notification.message = i18n.getTranslatedString('ToastCompletedMessage').format(outputPath, te - ts)
                    notification.send(False)
                if self.progressAnimation[3]:
                    self.progressbar.after_cancel(self.progressAnimation[3])
                    self.progressAnimation[3] = None
                self.vardoubleProgress.set(100)
            def failCallback(ex: Exception):
                if sys.platform != 'darwin':
                    notification.title = i18n.getTranslatedString('ToastFailedTitle')
                    notification.message = f'{type(ex).__name__}: {ex}'
                    notification.send(False)

            self.logFile = open(self.logPath, 'w', encoding='utf-8')
            t = threading.Thread(
                target=task.taskRunner,
                args=(
                    queue,
                    self.pauseEvent,
                    self.writeToOutput,
                    completeCallback,
                    failCallback,
                    lambda: (
                        self.varboolProcessing.set(False),
                        self.pauseEvent.set(),
                        self.buttonProcess.config(style='' if self.varboolProcessing.get() and not self.varboolProcessingPaused.get() else 'Accent.TButton'),
                        self.varstrLabelStartProcessing.set(i18n.getTranslatedString(('ContinueProcessing' if self.varboolProcessingPaused.get() else 'PauseProcessing') if self.varboolProcessing.get() else 'StartProcessing')),
                        self.logFile.close(),
                        sys.platform == 'win32' and self.progressNativeTaskbar.SetProgressState(int(self.master.wm_frame(), 16), 0), # TBPF_NOPROGRESS
                    ),
                    self.varboolIgnoreError.get(),
                )
            )
            t.start()
        except Exception as ex:
            messagebox.showerror(define.APP_TITLE, traceback.format_exc())

    def setInputPath(self, p: str):
        self.varstrInputPath.set(p)
        self.varstrOutputPath.set(self.getOutputPath(p))
        self.outputPathChanged = False

    def writeToOutput(self, s: str):
        if self.logFile:
            self.logFile.write(s)
        self.textOutput.config(state=tk.NORMAL)
        self.textOutput.insert(tk.END, s)
        self.textOutput.config(state=tk.DISABLED)
        yview = self.textOutput.yview()
        if yview[1] - yview[0] > .5 or yview[1] > .9:
            self.textOutput.see('end')

        # self.vardoubleProgress.set((self.progressValue[0] + self.progressValue[1]) / self.progressValue[2] * 100)
        progressFrom = self.vardoubleProgress.get()
        progressTo = (self.progressValue[0] + self.progressValue[1]) / self.progressValue[2] * 100
        if progressFrom != progressTo:
            def anim():
                if self.progressAnimation[3] is None:
                    return
                # print(f'Before anim {self.progressAnimation}')
                self.vardoubleProgress.set(self.progressAnimation[0] + (self.progressAnimation[1] - self.progressAnimation[0]) * (lambda x: 1 - (1 - x) ** 3)(self.progressAnimation[2]))
                self.progressAnimation[2] += 1 / 10
                if self.progressAnimation[2] < 1:
                    self.progressAnimation[3] = self.progressbar.after(10, anim)
                else:
                    self.progressAnimation[3] = None
                # print(f'After anim  {self.progressAnimation}')
            if self.progressAnimation[3]:
                afterId = self.progressAnimation[3]
                self.progressAnimation[3] = None
                self.progressbar.after_cancel(afterId)
                # print(f'Cancel {afterId}')
            self.progressAnimation[0] = progressFrom
            self.progressAnimation[1] = progressTo
            self.progressAnimation[2] = 0
            self.progressAnimation[3] = self.progressbar.after(10, anim)
            match sys.platform:
                case 'win32':
                    self.progressNativeTaskbar.SetProgressState(int(self.master.wm_frame(), 16), 2) # TBPF_NORMAL
                    self.progressNativeTaskbar.SetProgressValue(int(self.master.wm_frame(), 16), round(progressTo), 100)

    def getConfigParams(self) -> param.REConfigParams:
        resizeModeValue = 0
        match self.varintResizeMode.get():
            case param.ResizeMode.RATIO:
                resizeModeValue = self.varintResizeRatio.get()
            case param.ResizeMode.WIDTH:
                resizeModeValue = self.varintResizeWidth.get()
            case param.ResizeMode.HEIGHT:
                resizeModeValue = self.varintResizeHeight.get()
            case param.ResizeMode.LONGEST_SIDE:
                resizeModeValue = self.varintResizeLongestSide.get()
            case param.ResizeMode.SHORTEST_SIDE:
                resizeModeValue = self.varintResizeShortestSide.get()
        return param.REConfigParams(
            self.varstrModel.get(),
            self.modelFactors[self.varstrModel.get()],
            self.config['Config'].get('ModelDir') or os.path.join(define.APP_PATH, 'models'),
            self.varintResizeMode.get(),
            resizeModeValue,
            self.downsample[self.varintDownsampleIndex.get()][1],
            self.tileSize[self.varintTileSizeIndex.get()],
            self.varintGPUID.get(),
            self.varboolUseTTA.get(),
            self.varboolPreupscale.get(),
            self.varstrCustomCommand.get().strip(),
        )

    def getOutputPath(self, p: str) -> str:
        if os.path.isdir(p):
            base, ext = p, ''
        else:
            base, ext = os.path.splitext(p)
            if ext.lower() in {'.jpg', '.tif', '.tiff'} or self.varstrCustomCommand.get().strip():
                ext = '.png'
            if ext.lower() == '.png' and self.varboolUseWebP.get():
                ext = '.webp'
        suffix = ''
        match self.varintResizeMode.get():
            case param.ResizeMode.RATIO:
                suffix = f'x{self.varintResizeRatio.get()}'
            case param.ResizeMode.WIDTH:
                suffix = f'w{self.varintResizeWidth.get()}'
            case param.ResizeMode.HEIGHT:
                suffix = f'h{self.varintResizeHeight.get()}'
            case param.ResizeMode.LONGEST_SIDE:
                suffix = f'l{self.varintResizeLongestSide.get()}'
            case param.ResizeMode.SHORTEST_SIDE:
                suffix = f's{self.varintResizeShortestSide.get()}'
        return f'{base} ({self.models[self.comboModel.current()]} {suffix}){ext}'

# Config and model paths are initialized before main frame
# Because for the WarningNotFoundRE warning message app language
# must be initialized and for that config must be initialized
# and for that models variable needs to be set
def init_config_and_model_paths() -> tuple[configparser.ConfigParser, list[str]]:
    config = configparser.ConfigParser({
        'Upscaler': '',
        'ModelDir': '',
        'ResizeMode': int(param.ResizeMode.RATIO),
        'ResizeRatio': 4,
        'ResizeWidth': 1024,
        'ResizeHeight': 1024,
        'ResizeLongestSide': 1024,
        'ResizeShortestSide': 1024,
        'Model': '',
        'DownsampleIndex': 0,
        'GPUID': -1,
        'TileSizeIndex': 0,
        'LossyQuality': 80,
        'UseWebP': False,
        'UseTTA': False,
        'OptimizeGIF': False,
        'LossyMode': False,
        'IgnoreError': False,
        'Preupscale': False,
        'CustomCommand': '',
        'AppLanguage': locale.getdefaultlocale()[0],
    })
    config['Config'] = {}
    config.read(define.APP_CONFIG_PATH)

    if config['Config'].get('Upscaler'):
        define.RE_PATH = os.path.realpath(config['Config'].get('Upscaler'))

    try:
        modelDir = config['Config'].get('ModelDir') or os.path.join(define.APP_PATH, 'models')
        if os.path.splitext(os.path.split(define.RE_PATH)[1])[0] == 'realcugan-ncnn-vulkan':
            # 兼容Real-CUGAN的模型文件名格式
            # https://github.com/nihui/realcugan-ncnn-vulkan/blob/395302c5c70f1bff604c974e92e0a87e45c9f9ee/src/main.cpp#L733
            # -m model-path
            # -s scale
            # -n noise-level
            # <model-path>/up<scale>x-conservative.{param,bin}
            # <model-path>/up<scale>x-no-denoise.{param,bin}
            # <model-path>/up<scale>x-denoise<noise-level>x.{param,bin}
            models = []
            for name, scale, noise in itertools.product(
                sorted(x for x in os.listdir(modelDir) if os.path.isdir(os.path.join(modelDir, x))),
                range(2, 5),
                ('conservative', 'no-denoise', *(f'denoise{i}x' for i in range(1, 4))),
            ):
                if all(os.path.exists(os.path.join(modelDir, name, f'up{scale}x-{noise}.{ext}')) for ext in ('bin', 'param')):
                    models.append(f'{name}#up{scale}x-{noise}')
        else:
            modelFiles = set(x for x in os.listdir(modelDir) if os.path.isfile(os.path.join(modelDir, x)))
            models = sorted(
                x for x in set(os.path.splitext(y)[0] for y in modelFiles)
                if f'{x}.bin' in modelFiles and f'{x}.param' in modelFiles
            )
    except FileNotFoundError:
        # in case of FileNotFoundError exception, return empty modelFiles and models.
        # This does not change any behabiour because in this case
        # we will be showing a warning message and terminate app
        models = []

    i18n.set_current_language(config['Config'].get('AppLanguage'))
    return config, models

if __name__ == '__main__':
    os.chdir(define.APP_PATH)
    root = TkinterDnD.Tk(className=define.APP_TITLE)
    root.withdraw()

    config, models = init_config_and_model_paths()

    if not os.path.exists(define.RE_PATH) or not models:
        messagebox.showwarning(define.APP_TITLE, i18n.getTranslatedString('WarningNotFoundRE'))
        webbrowser.open_new_tab('https://github.com/xinntao/Real-ESRGAN/releases')
        sys.exit(0)

    root.title(define.APP_TITLE)
    try:
        root.iconbitmap(os.path.join(define.BASE_PATH, 'icon-256px.ico'))
    except tk.TclError:
        root.tk.call('wm', 'iconphoto', root._w, ImageTk.PhotoImage(Image.open(os.path.join(define.BASE_PATH, 'icon-256px.ico'))))

    root.tk.call('source', os.path.join(define.BASE_PATH, 'theme', 'sun-valley.tcl'))
    def changeTheme(theme: typing.Literal['Dark', 'Light']):
        root.tk.call('set_theme', 'dark' if theme == 'Dark' else 'light')
        # https://stackoverflow.com/questions/57124243/winforms-dark-title-bar-on-windows-10
        if sys.platform == 'win32':
            match sys.getwindowsversion().build:
                case build if build >= 18985:
                    attribute = 20
                case build if build >= 17763:
                    attribute = 19
                case _:
                    attribute = None
            if attribute:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    ctypes.windll.user32.GetParent(root.winfo_id()),
                    attribute,
                    ctypes.byref(ctypes.c_int(theme == 'Dark')),
                    ctypes.sizeof(ctypes.c_int),
                )
    try:
        import darkdetect
        changeTheme(darkdetect.theme())
        if sys.platform in {'win32', 'linux'}:
            t = threading.Thread(target=darkdetect.listener, args=(changeTheme,))
            t.daemon = True
            t.start()
    except:
        print(traceback.format_exc())
        changeTheme('Light')

    app = REGUIApp(root, config, models)
    app.drop_target_register(DND_FILES)
    app.dnd_bind(
        '<<Drop>>',
        lambda e: app.setInputPath(e.data[1:-1] if '{' == e.data[0] and '}' == e.data[-1] else e.data),
    )
    app.pack(fill=tk.BOTH, expand=True)
    root.protocol('WM_DELETE_WINDOW', lambda: (
        app.close(),
        root.destroy(),
    ))

    initialSize = (720, 640)
    root.minsize(*initialSize)
    root.geometry('{}x{}+{}+{}'.format(
        *initialSize,
        (root.winfo_screenwidth() - initialSize[0]) // 2,
        (root.winfo_screenheight() - initialSize[1]) // 2,
    ))

    # 最好用的一个 要是第一次通过拖放打开文件路径就好了 · Issue #45 · TransparentLC/realesrgan-gui
    # https://github.com/TransparentLC/realesrgan-gui/issues/45
    if len(sys.argv) > 1:
        app.setInputPath(sys.argv[1])

    root.deiconify()
    root.mainloop()
