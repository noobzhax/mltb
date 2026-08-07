# Debug Video Duration Mismatch - Testing Instructions

## 📋 Current Status

**Problem**: Download hasil memiliki durasi tidak match dengan video asli
- **Original video**: ~12 menit (claimed 177 segments)
- **Downloaded result**: Shorter duration (details needed)

**Fixes Already Applied**:
1. ✅ Token expiry handling (retry + resume)
2. ✅ Real-time speed indicator  
3. ✅ High-quality variant selection
4. ✅ **NEW**: Comprehensive debug logging (just deployed!)

---

## 🧪 Test Now - Get Full Debug Info

### Step 1: Send Vidara Link to Bot
```
https://vidara.so/v/1Vx05roBvotbV
```

### Step 2: Watch Detailed Logs in Real-time
```bash
docker logs -f mltb-app-1 --since 3m | grep "\[Vidara\]"
```

**OR** after download completes:
```bash
docker logs mltb-app-1 --tail 200 | grep "\[Vidara\]" > vidara_debug.log
cat vidara_debug.log
```

---

## 🔍 What We're Looking For

The debug logs will show us EXACTLY what's happening:

### Critical Log Messages to Look For:

```
[Vidara] Starting to fetch master playlist...
[Vidara] Master playlist content preview:
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
...
[Vidara] Found N lines in master playlist

[Vidara] Variant #0: https://cdn.example.com/playlist_1080.m3u8
[Vidara] Variant #1: https://cdn.example.com/playlist_720.m3u8
[Vidara] Variant #2: https://cdn.example.com/playlist_480.m3u8

[Vidara] Total valid variant URLs found: 3

[Vidara] Selected variant: https://... (matched quality '1080')
       ^^^^ ← THIS IS CRITICAL! Which variant was selected?

[Vidara] Fetching media playlist (attempt 1/3)...
[Vidara] ✅ Media playlist fetched successfully!
[Vidara] 📊 Total segments: 177
       ^^^^ ← THIS IS CRITICAL! Full segment count?

[Vidara] First 5 segments:
[Vidara]   Segment 0: https://cdn.example.com/seg_000.ts
[Vidara]   Segment 1: https://cdn.example.com/seg_001.ts
[Vidara]   ...

[Vidara] Downloading 177 segments (retry 1/3)
[Vidara] ➡️ Resuming from segment X/177

[Vidara] 🎯 All segments already downloaded successfully!

✅ Download completed: Al 033106358.mp4
```

---

## 🎯 Diagnosis Scenarios

### Scenario A: ❌ WRONG VARIANT SELECTED
**Symptoms**:
```
[Vidara] Selected variant: playlist_low.m3u8 (no quality detected)
[Vidara] 📊 Total segments: 95    ← SHORTER than 177!
```

**Cause**: Bot auto-selected low-quality stream without quality keywords

**Action**: Update quality detection logic OR manually specify quality preference

---

### Scenario B: ⚠️ INCOMPLETE DOWNLOAD
**Symptoms**:
```
[Vidara] 📊 Total segments: 177
[Vidara] Downloading 177 segments (retry 2/3)
[Vidara] ➡️ Resuming from segment 142/177
         ↓ But download stops early due to token expiry
```

**Cause**: Token expired mid-download despite retry logic

**Action**: Check if token has longer TTL OR increase retry attempts

---

### Scenario C: ❓ MULTIPLE MASTER PLAYLISTS
**Symptoms**:
```
[Vidara] Master playlist content preview:
#EXT-X-STREAM-INF:BANDWIDTH=500000  ← Low quality only?
playlist_low.m3u8

[Vidara] Selected variant: playlist_low.m3u8 (no quality detected)
```

**Cause**: API hanya return low-quality variants

**Action**: Contact Vidara API provider OR use different extraction method

---

### Scenario D: ✅ NORMAL OPERATION - Need More Info
**Symptoms**:
```
[Vidara] Selected variant: playlist_hd.m3u8 (matched quality 'hd')
[Vidara] 📊 Total segments: 177    ← Full count!
[Vidara] ✅ All segments downloaded successfully!
```

**But Duration Still Mismatch?**
- Could be ffmpeg concat issue
- Could be missing segments in final output
- Need to verify with ffprobe

**Action**: Run ffprobe on downloaded file:
```bash
docker exec mltb-app-1 ffprobe -v error -show_entries format=duration \
  -of default=noprint_wrappers=1:nokey=1 /app/downloads/<file>.mp4
```

Compare this with original video duration from Vidara player.

---

## 📤 Share This Info for Further Help

After running the test, please share:

1. **Full debug log output** (from `docker logs` command)
2. **Original video duration** (from Vidara player or source)
3. **Downloaded file duration** (if you have it locally)
4. **Which variant was selected** (from logs)
5. **Total segment count** (from logs)

Format like this:
```
=== DEBUG LOG START ===
[Vidara] Started fetching playlist at 01:38:00
[Vidara] Found 3 lines in master playlist
[Vidara] Variant #0: https://cdn.xxx/playlist_1080p.m3u8
[Vidara] Variant #1: https://cdn.xxx/playlist_720p.m3u8  
[Vidara] Variant #2: https://cdn.xxx/playlist_480p.m3u8
[Vidara] Selected variant: https://cdn.xxx/playlist_1080p.m3u8 (matched quality '1080')
[Vidara] ✅ Media playlist fetched successfully!
[Vidara] 📊 Total segments: 177
...
=== DEBUG LOG END ===

Original video duration: 12:03 minutes
Downloaded file duration: ? minutes
Selected variant: HD (1080p)
Segment count: 177 (full)
```

---

## 🔬 Alternative Tests

### Test 1: Different Vidara Links
Try other Vidara videos to see if issue is specific to this one:
```
https://vidara.so/v/[other_video_id]
```

### Test 2: Manual FFmpeg Verification
If you can get downloaded file:
```bash
# Get video info
ffprobe downloaded.mp4

# Get segment count from original m3u8 (if accessible)
curl "original_playlist_url" | grep -c ".ts$"
```

### Test 3: Compare File Sizes
Large file sizes usually indicate full download:
```bash
ls -lh downloaded.mp4
# Should be ~100MB+ for 12-min HD video
```

---

## 💡 Quick Questions to Answer

Before we proceed, please answer:

1. **How many segments were actually downloaded?** (check logs)
2. **Which quality variant was selected?** (check logs)
3. **What's the final file duration vs original?**
4. **Is this happening consistently or intermittently?**
5. **Other Vidara links also have same issue?**

Once you provide debug logs and answers, I can pinpoint exact cause! 🔍
