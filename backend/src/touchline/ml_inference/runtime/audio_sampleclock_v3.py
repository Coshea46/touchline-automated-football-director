"""Versioned audio sample-clock reconstruction and strict independent validation.

This helper does not replace the corrected public decoder or mutate any stores.
Matroska/Vorbis timestamps can include *two* quantization stages: observed frame
PTS jitter exceeds one 1ms container tick even when decoded sample counts are
continuous. Resetting the resampler clock at each such tick creates waveform
jumps. For that codec/container combination we reconstruct the continuous native
sample clock within an explicit three-tick envelope; larger discontinuities stay
real gaps. Independent waveform validation still requires correlation >= .95 and
absolute lag <=64 samples at16kHz. A low correlation never lowers those gates.
"""

from fractions import Fraction
import subprocess

import av
import numpy as np
from scipy.signal import correlate

REVISION = "autonomous-native-sample-clock-v1"


def decode_audio_sampleclock(path,start,end,video_origin,*,return_audit=False):
    """Causal observation PCM: crop native decoded samples BEFORE resampling.

    Actual unavailable time remains NaN. No labels, event time or samples past
    ``end`` enter the output. Decoding codec frames that cross ``end`` is allowed,
    but their samples beyond the endpoint are discarded before filtering.
    """
    if end<=start:raise ValueError('Positive audio observation duration required')
    rate=16000
    audit={'revision':REVISION,'clock_corrections':0,'real_discontinuities':[],
           'maximum_corrected_pts_error_seconds':0.0,'input_start_seconds':float(start),
           'input_end_seconds':float(end),'video_origin_seconds':float(video_origin),
           'crop_before_resample':True}
    with av.open(str(path)) as container:
        if not container.streams.audio:
            return (None,None,audit) if return_audit else (None,None)
        stream=container.streams.audio[0]
        channels=len(stream.codec_context.layout.channels)
        if channels not in (1,2):raise ValueError('Only original mono/stereo audio supported')
        audit.update(codec=stream.codec_context.name,time_base=str(stream.time_base),native_sample_rate=stream.codec_context.sample_rate)
        container.seek(max(0,int((video_origin+start-.25)*av.time_base)),backward=True)
        resampler=av.AudioResampler(format='fltp',layout='mono' if channels==1 else 'stereo',rate=rate)
        waveform=np.full((round((end-start)*rate),channels),np.nan,np.float32)
        def store(frame):
            if frame.pts is None:raise ValueError('Resampled audio has no PTS')
            t=float(frame.pts*frame.time_base)-video_origin
            data=np.clip(frame.to_ndarray().T,-1,1)
            offset=round((t-start)*rate);a=max(0,offset);b=min(len(waveform),offset+len(data))
            if b>a:waveform[a:b]=data[a-offset:b-offset]
        next_sample_pts=None
        for frame in container.decode(stream):
            if frame.pts is None:raise ValueError('Source audio has no PTS')
            measured=round(float(frame.pts*frame.time_base)*frame.sample_rate)
            ticks=3 if stream.codec_context.name=='vorbis' and .00099<=float(stream.time_base)<=.00101 else 1
            tolerance=max(1,int(np.ceil(ticks*float(frame.time_base)*frame.sample_rate)))
            sample_pts=measured
            if next_sample_pts is not None:
                error=measured-next_sample_pts
                if abs(error)<=tolerance:
                    sample_pts=next_sample_pts
                    if error:
                        audit['clock_corrections']+=1
                        audit['maximum_corrected_pts_error_seconds']=max(audit['maximum_corrected_pts_error_seconds'],abs(error)/frame.sample_rate)
                else:
                    audit['real_discontinuities'].append({'at_seconds':measured/frame.sample_rate-video_origin,'gap_seconds':error/frame.sample_rate})
            next_sample_pts=sample_pts+frame.samples
            frame_time=sample_pts/frame.sample_rate
            if frame_time-video_origin>=end:break
            a=max(0,int(np.ceil((video_origin+start-frame_time)*frame.sample_rate-1e-7)))
            b=min(frame.samples,int(np.ceil((video_origin+end-frame_time)*frame.sample_rate-1e-7)))
            if b<=a:continue
            raw=frame.to_ndarray();native=raw[:,a:b] if frame.format.is_planar else raw[:,a*channels:b*channels]
            clipped=av.AudioFrame.from_ndarray(np.ascontiguousarray(native),format=frame.format.name,layout=frame.layout.name)
            clipped.sample_rate=frame.sample_rate;clipped.time_base=Fraction(1,frame.sample_rate);clipped.pts=sample_pts+a
            for converted in resampler.resample(clipped):store(converted)
        for converted in resampler.resample(None):store(converted)
    audit['finite_sample_fraction']=float(np.isfinite(waveform).all(axis=1).mean())
    result=waveform[:,0] if channels==1 else waveform
    return (result,rate,audit) if return_audit else (result,rate)


def independent_check(path,start,end,origin,*,ffmpeg='ffmpeg'):
    """Check source offset/PCM on an observation; never change failed thresholds."""
    wave,rate,audit=decode_audio_sampleclock(path,start,end,origin,return_audit=True)
    if wave is None:raise ValueError('Independent PCM check requires an audio stream')
    channels=1 if wave.ndim==1 else wave.shape[1]
    command=[str(ffmpeg),'-v','error','-seek_timestamp','1','-ss',str(start+origin),'-i',str(path),
             '-map','0:a:0','-t',str(end-start),'-vn','-ac',str(channels),'-ar',str(rate),'-f','f32le','pipe:1']
    raw=subprocess.run(command,capture_output=True,check=True,timeout=60).stdout
    reference=np.frombuffer(raw,dtype='<f4').reshape(-1,channels)
    count=min(len(reference),len(wave));trim=round(.1*rate)
    if count<=2*trim:raise ValueError('Insufficient independent audio support')
    decoded=wave.reshape(-1,channels)
    checks=[]
    for channel in range(channels):
        a=decoded[trim:count-trim,channel].astype(float);b=reference[trim:count-trim,channel].astype(float)
        # Zero filling is used only in a diagnostic correlation to locate lag.
        # It never enters stored PCM/features. Final correlation excludes NaNs.
        filled=np.nan_to_num(a);filled-=filled.mean();bb=b-b.mean()
        lag=int(np.argmax(correlate(filled,bb,method='fft'))-len(bb)+1)
        aa,bb=(a[lag:],b[:-lag]) if lag>0 else (a[:lag],b[-lag:]) if lag<0 else (a,b)
        finite=np.isfinite(aa)&np.isfinite(bb);aa=aa[finite];bb=bb[finite]
        aa-=aa.mean();bb-=bb.mean()
        energy=float(np.linalg.norm(aa)*np.linalg.norm(bb))
        if energy<1e-12:raise ValueError('Silent/degenerate comparison cannot verify waveform alignment')
        correlation=float(np.dot(aa,bb)/energy)
        checks.append({'channel':channel,'lag_samples_16khz':lag,'correlation':correlation,'finite_compared_samples':len(aa)})
    passed=all(abs(c['lag_samples_16khz'])<=64 and c['correlation']>=.95 for c in checks)
    return {'passed':passed,'minimum_correlation':.95,'maximum_absolute_lag_samples':64,
            'checks':checks,'decoder_audit':audit,'reference':'Independent FFmpeg CLI accurate input seek; waveform gate excludes only 100ms boundary/filter transients'}
