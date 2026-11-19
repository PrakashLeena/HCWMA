# Step 14 — STT benchmark (owner's own recordings)

faster-whisper, CPU, int8, greedy (beam 1), VAD on. 8 logical CPUs, 15.7 GB RAM (4.4 GB free when run).
Accuracy: WER = word error rate, CER = character error rate (lower is better; CER is the fairer score for Tamil and Sinhala). Lang ID = clips where auto-detect picked the right language.

| Model | Language | Clips | Mean s / 5 s clip | Max s | WER | CER | Lang ID | Peak RAM MB |
|---|---|---|---|---|---|---|---|---|
| tiny | English | 5 | 0.50 | 0.94 | 19% | 12% | 4/5 | 280 |
| tiny | Tamil | 5 | 0.93 | 3.15 | 123% | 76% | 1/5 | 280 |
| tiny | Sinhala | 1 | 0.97 | 0.97 | 100% | 100% | 0/1 | 280 |
| base | English | 5 | 1.01 | 2.19 | 46% | 19% | 4/5 | 445 |
| base | Tamil | 5 | 2.19 | 7.37 | 106% | 68% | 0/5 | 445 |
| base | Sinhala | 1 | 1.87 | 1.87 | 100% | 100% | 0/1 | 445 |
| small | English | 5 | 2.37 | 2.68 | 19% | 12% | 3/5 | 1038 |
| small | Tamil | 5 | 4.18 | 10.18 | 72% | 44% | 4/5 | 1038 |
| small | Sinhala | 1 | 45.53 | 45.53 | 133% | 148% | 0/1 | 1038 |

Model load time and RAM added by the model:

- tiny: load 0.44 s, +96 MB
- base: load 0.61 s, +140 MB
- small: load 1.6 s, +326 MB

## What each model heard

### tiny

- `en/01.wav` CER 3%: Show me the files in my download folder
- `en/02.wav` CER 12%: Remind me tomorrow at 9 in the morning.
- `en/03.wav` CER 41%: read my last statement.
- `en/04.wav` CER 0%: make this photo smaller.
- `en/05.wav` CER 5%: There is my invoice file.
- `ta/01.wav` CER 75%: நானுடன் பக்கிறீர்களும் நிலைக்குப் புத்துள்ளிக்காட்டேன்.
- `ta/02.wav` CER 86%: நான் அல்லை காலியும் ஒரு மனிக்கிய இருக்கிறீர்களும் உட்டேன்.
- `ta/03.wav` CER 56%: ஏன் ராமி வதியமின் நீங்கள் வடி.
- `ta/04.wav` CER 82%: இங்கு இப்போது இப்போது இப்போது இந்த அழைக் குழை.
- `ta/05.wav` CER 81%: என்னுடைய செய்துகிறீர்கள்.
- `si/02.wav` CER 100%: 来, 来, 来, 来

### base

- `en/01.wav` CER 12%: Show me the files in mine, download folder.
- `en/02.wav` CER 12%: Remind me tomorrow at 9 in the morning.
- `en/03.wav` CER 12%: Read my last e-mail.
- `en/04.wav` CER 0%: Make this photo smaller.
- `en/05.wav` CER 60%: The Irish man in wise faith.
- `ta/01.wav` CER 55%: நான் உன்னையில்லைக் கோப்பலைக் காட்டி
- `ta/02.wav` CER 32%: நாளை காலை ஒன்பர் மனிக்கினைக் கிடி நீ முட்டு.
- `ta/03.wav` CER 70%: என்றாம் இப்பத்தியும் மீங்கள் மின்னங்களைப் படி
- `ta/04.wav` CER 43%: இண்ண போய் படாத்தின் அலவே குரை
- `ta/05.wav` CER 142%: எல்லையும் செல்லுங்கள் உன்னை நின்னை விஷயாக இருக்கிறேன்.
- `si/02.wav` CER 100%: Pallima

### small

- `en/01.wav` CER 39%: Show me the files in mind down below.
- `en/02.wav` CER 12%: Remind me tomorrow at 9 in the morning.
- `en/03.wav` CER 6%: Read my lastest email.
- `en/04.wav` CER 0%: Make this photo smaller
- `en/05.wav` CER 5%: Here is my invoice file
- `ta/01.wav` CER 41%: இந்தான் உன்னுடைய பார்த்தில் உள்ள கோப்புகளைக் காட்டி.
- `ta/02.wav` CER 49%: நாளைக் காலை 9 மணிக்கியின் நீ மூட்டு
- `ta/03.wav` CER 48%: என் சமிபத்திய மின்னஞ்சில் மின்னஞ்சில் படி
- `ta/04.wav` CER 25%: இன்னே போய்ப்படத்தின் அடவைக் குறை
- `ta/05.wav` CER 58%: and invoice file is here.
- `si/02.wav` CER 148%: අසු වීයක නිල් නුර ස්නායි සොීම වීදි ස්ලුරඉ

