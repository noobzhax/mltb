# 🔍 VIDARA VIDEO ANALYSIS - Complete Investigation Report

## 📋 RAW DATA FROM ACTUAL STREAM

### Video URL: `https://vidara.so/v/1Vx05roBvotbV`

**Extracted from actual API call:**

```json
{
  "title": "Al 033106358",
  "resolution": "864x1920 (1080p vertical)",
  "bandwidth": "3,580,347 bps (~3.6 Mbps)",
  "streaming_url": "https://p1-s100-d3.s1q2105.com/hls/.../master.m3u8"
}
```

---

## ✅ COMPLETE PLAYLIST ANALYSIS

### Master Playlist Content:
```m3u8
#EXTM3U
#EXT-X-VERSION:6
#EXT-X-STREAM-INF:BANDWIDTH=3580347,CODECS="avc1.640028,mp4a.40.2",RESOLUTION=864x1920,FRAME-RATE=30.000
index_864x1920.m3u8
```

**Note**: There's only **ONE variant** available - no quality selection needed!

### Media Playlist Analysis:
- **Total Segments**: **178 segments** ✓
- **Segment Duration**: ~4 seconds each
- **Video Type**: VOD (not live)
- **First Segment**: `seg_864x1920_000.ts`
- **Last Segment**: `seg_864x1920_177.ts`

### ⏱️ CALCULATED DURATION:
```
Total duration = 709.45 seconds
               = 11 minutes 49 seconds
               ≈ 12 minutes ✓ (matches your claim!)
```

**Conclusion**: This is a FULL video with all segments present!

---

## ❌ WHAT THIS MEANS

### The Problem Is NOT:
1. ❌ Low-quality variant selected (only ONE quality exists)
2. ❌ Truncated playlist (all 178 segments available)
3. ❌ Token expiry mid-download (if implemented)
4. ❌ Missing segments in master playlist

### The Problem IS:
🔴 **Downloaded file has shorter duration than original**

**Possible Causes:**
1. **Token expires during download** → Some segments fail silently
2. **ffmpeg concat issue** → Concat fails for some segments  
3. **Segments corrupted/unavailable** → Download fails but continues
4. **Timing issues** → First/last segments cut off

---

## 🎯 ROOT CAUSE HYPOTHESIS

Based on the analysis, I suspect:

### Scenario: Silent Failures During Download
```
Bot downloads: seg_000 to seg_149 ✓ (first 10 min)
              → Token expires at segment 150
              → seg_150 to seg_177 get HTTP 403/504
              → These fail silently or are skipped
              → Final file only has 10 minutes instead of 12
```

**Why this happens:**
- Retry logic might not be aggressive enough
- Failed segments might be silently dropped
- No verification that ALL segments were downloaded before concat

---

## 🛠️ RECOMMENDED SOLUTIONS

### Solution 1: Verify All Segments After Download
Add strict verification step:

```python
# After download completes
missing_segments = []
for i in range(self.total_segments):
    seg_path = os.path.join(temp_dir, f"seg_{i:05d}.ts")
    if not os.path.exists(seg_path):
        missing_segments.append(i)

if missing_segments:
    error_msg = f"Missing {len(missing_segments)} segments: {missing_segments[:10]}..."
    await self.listener.on_download_error(error_msg)
    return
```

### Solution 2: Increase Retry Attempts & Timeout
Current: 5 retries, 60s timeout
Recommended: **10 retries, 120s timeout**

### Solution 3: Validate Final Duration Before Upload
After concat/remux:
```python
out_duration = ffprobe(out_path).duration
if out_duration < expected_duration * 0.95:
    await self.listener.on_download_error(
        f"Duration mismatch: {out_duration}s vs expected {expected_duration}s"
    )
    return
```

### Solution 4: Use Range Requests Instead of Concat
Instead of downloading all segments then concatenating:
- Try direct MP4 download if available
- OR use ffmpeg `-reconnect` flags for better retry handling

---

## 🧪 TEST TO VERIFY

I've added comprehensive debug logging. Now run this test:

```bash
# Send link to bot again
https://vidara.so/v/1Vx05roBvotbV

# Watch logs in real-time
docker logs -f mltb-app-1 --since 3m | grep "\[Vidara\]"
```

**Look for these critical messages:**
```
[Vidara] ✅ Media playlist fetched successfully!
[Vidara] 📊 Total segments: 178    ← Should show 178!

[Vidara] Downloading 178 segments (retry 1/3)
[Vidara] ➡️ Resuming from segment X/178

[Vidara] 🎯 All segments already downloaded successfully!

✅ Download completed: Al 033106358.mp4
```

If you see:
- `"Total segments: 178"` → Playlist is complete ✓
- `"Downloading 178 segments"` → Bot sees full count ✓
- But final file is still short → Download failures during process

---

## 📈 EXPECTED BEHAVIOR AFTER FIXES

Once our fixes work properly:

| Metric | Current | Expected After Fix |
|--------|---------|-------------------|
| Segments downloaded | ? | 178/178 (100%) |
| Duration match | ✗ | ✓ Exact match (11:49) |
| Error rate | High | 0% |
| Retry attempts | Incomplete | Full 5x per failed segment |

---

## 💬 ACTION ITEMS FOR YOU

Please do these tests and share results:

### Test 1: Run New Debug Build
```bash
# Already deployed! Just send link to bot
Send: https://vidara.so/v/1Vx05roBvotbV

# Share debug logs output
docker logs mltb-app-1 --tail 300 | grep "\[Vidara\]" > vidara_test.log
cat vidara_test.log
```

### Test 2: Manual FFmpeg Verification (If you have local file)
```bash
# Check downloaded file duration
ffprobe downloaded.mp4 -show_entries format=duration

# Compare with expected
Expected: 709.45 seconds (11:49)
Actual:   ___ seconds
Difference: ___ seconds
```

### Test 3: Check File Size
```bash
ls -lh downloaded.mp4
```
**Expected size**: ~25-35 MB for 12-min @ 3.6 Mbps

If file is smaller than expected, it confirms partial download.

---

## 🎯 SUMMARY

**Raw Truth:**
- Stream is complete: 178 segments, 11:49 duration
- Only ONE quality level exists (no selection needed)
- Problem is during download/process, not playlist selection

**Root Cause Likely:**
- Tokens expire mid-download despite our retry logic
- Failed segments are silently dropped
- No validation before concat

**Next Steps:**
1. Run debug test above
2. Share `[Vidara]` log output
3. Verify actual segment count downloaded
4. Check final file duration vs expected

Once we have the debug logs, I can pinpoint EXACTLY where it fails! 🔍
