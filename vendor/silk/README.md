# silk_v3_decoder.exe

来源: kn007/silk-v3-decoder — MinGW 编译的 Skype SILK v3 Codec.

- Release page: https://github.com/kn007/silk-v3-decoder/releases/tag/win
- Download URL: https://github.com/kn007/silk-v3-decoder/releases/download/win/silk2mp3.zip
- 原压缩包 SHA1: `ED672C2C231773547AE9CCECCF043819304CFE44`
- 我们只从里面抽出了 `silk_v3_decoder.exe` (179037 B)

用途
----
把 PC 企业微信 `Cache/Voice/*.silk` (SILK V3 编码, 头字节 `\x02#!SILK_V3`) 解码成
raw PCM (`pcm_s16le`, 24000 Hz, mono), 再由 `vendor/ffmpeg/ffmpeg.exe` 封成 WAV,
喂给虚拟麦克风 (spike1 路径).

命令
----
```
silk_v3_decoder <input.silk> <output.pcm>
ffmpeg -f s16le -ar 24000 -ac 1 -i <output.pcm> <output.wav>
```

杀软误报
--------
少数杀毒软件会把这个 exe 报为 riskware (因为是 MinGW 编译且带 SILK 编码器).
kn007 已在 release note 声明 "杀毒软件用好点的牌子". 我们的 SHA1 与官方一致.
