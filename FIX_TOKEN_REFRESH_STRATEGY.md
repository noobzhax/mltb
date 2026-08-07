# 🔧 CRITICAL FIX: Token Refresh on Expiry

## 🐛 Problem Analysis

**Error**: "Download incomplete: 150 segments missing (28/178)"

**Root Cause**: 
- Only first 28 segments downloaded successfully (~2 min)
- Next 150 segments failed due to token expiry
- Code retries each failed segment 5x but uses SAME expired token
- No mechanism to get fresh token mid-download

## ✅ Solution: Auto-Token Refresh

When we detect HTTP 403/404/504, immediately:
1. Call Vidara API again to get NEW token
2. Update master_url with fresh token
3. Refetch playlist with new token  
4. Resume download

### Implementation Plan

```python
async def _download_segment(self, client, url, index, temp_dir):
    for attempt in range(5):
        try:
            async with client.stream("GET", url) as response:
                if status_code in (403, 404, 504):
                    if attempt < 4:  # Not last attempt
                        # REFRESH TOKEN IMMEDIATELY!
                        await self._refresh_token_and_playlist(client)
                        continue
                    
                    raise Exception("Token expired")
                    
                # Normal download
                ...
```

### Key Changes Needed:

1. Add `_refresh_token_and_playlist()` method:
   - Call VIDARA_API with filecode
   - Get new streaming_url (with new token)
   - Update self._master_url
   - Refetch media playlist
   - Return updated segment URLs

2. Detect token expiry in segment download:
   - On first 403/404/504 → trigger token refresh
   - Continue retrying with fresh token

3. Track which segments succeeded before expiry:
   - Resume from where we left off after refresh

---

**This is a major fix - let me implement it now!**
