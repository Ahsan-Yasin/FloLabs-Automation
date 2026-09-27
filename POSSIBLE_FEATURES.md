# Possible New Features

Running list of ideas to consider later. Nothing here is implemented yet — add to this
file as new ideas come up.

- Add a short summary/overview of the whole meeting at the start of the output video.
- Add timestamps for users (so they can see where in the original recording each part came from).
- Maybe help marketing by gathring funny clips or higlighted clips  
- Make an automation where the summary or the  whole transcript is store in a vector DB and when someone misses a meeting he can just chat via a chatbot to learn about specific stuff  This would save alot  of time 


how to fix transiastions 

seperate noise vedio with time stamps 

add higlisht amybe 4-5 mins  to the beigning 

also make some reels tuff form that  
we need to do all nthis in 1 go   
make intro + highlghts +   cleaned vedio and shrot clips   

would need an api key    , zooms api key   ,  a member from   content creation team  


---

## Status (2026-09-27)

Shipped from the list above:
- Highlights (up to about 5 minutes) at the start, then the cleaned meeting: `final.mp4`.
- Timestamps back to the original recording: `removed.mp4` labels, `transcript_removed`, the report.
- Clips for marketing: vertical shorts with captions and titles (learning moments first, funny ones capped).
- Transitions: 0.5 s dissolves centred on each cut, frame-exact.
- A separate video of the removed parts with timestamps and reasons: `removed.mp4`.
- Everything in one go: one job, one `bundle.zip`.
- API keys: per-user keys with scopes, signed webhooks, `/api/v1`.
- Zoom's API was built, then removed from `prod` (YouTube links and uploads only).

Still open:
- A short spoken or written summary of the whole meeting at the start of the video.
- Chat with past meetings: transcripts in a vector database behind a chatbot.
- Paid plans with billing (plan limits exist; there is no checkout).
- Deliver bundles to S3 or Google Drive instead of downloading from the server.
- Team workspaces: share jobs within a company account.
- Periodic retention sweep (today it runs only at startup).
- Mark transient YouTube download failures as retryable.
- Pin the resolved address when sending webhooks (closes the last DNS-rebinding gap).
- Browser end-to-end tests (sign up, verify, cut, download) and a Lighthouse check in CI.
