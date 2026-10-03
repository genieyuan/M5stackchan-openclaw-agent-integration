"""Local speech for the StackChan edge: SenseVoice STT, Kokoro (en/zh) + VITS Cantonese TTS.
Everything runs in-process on the edge host from pinned files; no network access."""
import os
import re
import numpy as np
import sherpa_onnx

M = os.environ.get("STACKCHAN_MODELS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models"))
SV = f"{M}/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
KO = f"{M}/kokoro-multi-lang-v1_0"
YUE = f"{M}/vits-cantonese-hf-xiaomaiiwn"

# Kokoro v1.0 multi-lang: sid 3 = af_heart (English), 47 = zf_xiaoxiao (Mandarin).
VOICE = {"en": int(os.environ.get("STACKCHAN_SID_EN", "3")), "zh": int(os.environ.get("STACKCHAN_SID_ZH", "47"))}
THREADS = 4


class STT:
    def __init__(self):
        self.r = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=f"{SV}/model.int8.onnx", tokens=f"{SV}/tokens.txt", num_threads=THREADS,
            use_itn=True, language="auto")

    def transcribe(self, pcm16k: np.ndarray):
        s = self.r.create_stream()
        s.accept_waveform(16000, pcm16k.astype(np.float32))
        self.r.decode_stream(s)
        res = s.result
        text = res.text.strip()
        if text and text.upper() == text and not _CJK.search(text):
            text = text.capitalize()  # SenseVoice emits English in capitals
        # SenseVoice's own language tag proved unreliable (tags English as yue), so judge from the text.
        return text, guess_lang(text)


class TTS:
    def __init__(self):
        self.ko = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=f"{KO}/model.onnx", voices=f"{KO}/voices.bin", tokens=f"{KO}/tokens.txt",
                    data_dir=f"{KO}/espeak-ng-data", dict_dir=f"{KO}/dict",
                    lexicon=f"{KO}/lexicon-us-en.txt,{KO}/lexicon-zh.txt"),
                num_threads=THREADS),
            rule_fsts=f"{KO}/date-zh.fst,{KO}/phone-zh.fst,{KO}/number-zh.fst", max_num_sentences=1))
        self.yue = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=f"{YUE}/vits-cantonese-hf-xiaomaiiwn.onnx", lexicon=f"{YUE}/lexicon.txt",
                    tokens=f"{YUE}/tokens.txt"),
                num_threads=THREADS),
            rule_fsts=f"{YUE}/rule.fst", max_num_sentences=1))

    def synth(self, text: str, lang: str, speed: float = 1.0):
        """Returns (float32 samples, sample_rate)."""
        if lang == "yue":
            a = self.yue.generate(text, sid=0, speed=speed)
        else:
            a = self.ko.generate(text, sid=VOICE["zh" if lang == "zh" else "en"], speed=speed)
        return np.asarray(a.samples, dtype=np.float32), a.sample_rate


_CJK = re.compile(r"[一-鿿]")
_YUE_MARKERS = re.compile(r"[嘅咗喺啲冇乜嘢咩唔佢哋嗰噉囉喎吖]|鍾意|钟意|而家|聽日|听日|點解|点解|邊個|边个|係咪|系咪")


def guess_lang(text: str, stt_lang: str = "") -> str:
    """Language of a reply: Cantonese if the child spoke Cantonese or the text has Cantonese particles."""
    if not _CJK.search(text):
        return "en"
    if stt_lang == "yue" or _YUE_MARKERS.search(text):
        return "yue"
    return "zh"


_SENT = re.compile(r"[^.!?。！？；;]*[.!?。！？；;]+[\"'”’」』)）]*\s*|[^.!?。！？；;]+$")


def sentences(text: str, max_len: int = 160):
    out = []
    for part in _SENT.findall(text):
        part = part.strip()
        while len(part) > max_len:
            cut = max(part.rfind(",", 0, max_len), part.rfind("，", 0, max_len), part.rfind(" ", 0, max_len))
            cut = cut if cut > 20 else max_len
            out.append(part[:cut + 1].strip())
            part = part[cut + 1:].strip()
        if part:
            out.append(part)
    return out
