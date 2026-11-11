"""Offline tests for integration ml_inference features test_audio_parity."""


import importlib.util
import json
import subprocess
import sys
from zipfile import ZipFile
import pytest
from tests.paths import ML_FIXTURES, ML_ROOT


FIXTURES = ML_FIXTURES / 'audio_v3'


RUNTIME = ML_ROOT / 'runtime'


def _released_wheel(tmp_path):
    wheels = list(RUNTIME.glob('matchpulse-*autonomous*.whl'))
    assert len(wheels) == 1, 'The verified autonomous runtime wheel must be packaged for v3 admission'
    package = tmp_path / 'released'
    with ZipFile(wheels[0]) as wheel:
        wheel.extractall(package)
    return package


def _av_python():
    if importlib.util.find_spec('av'):
        return sys.executable
    pytest.skip('A CPU PyAV environment is required for native decoder parity')


def test_real_release_pcm_to_core5_matches_published_raw_preserving_visual_bytes(tmp_path):
    package = _released_wheel(tmp_path)
    script = '''
import json,sys
sys.path.insert(0,sys.argv[1])
import numpy as np
from touchline.ml_inference.features.audio import repair_v3_audio
pcm=np.load(sys.argv[2],allow_pickle=False)
expected=np.load(sys.argv[3],allow_pickle=False)
raw=expected.copy(); raw[:,210:]=np.nan
before=raw.copy()
actual=repair_v3_audio(raw,pcm,endpoint_seconds=5.0)
assert actual[:,:210].tobytes()==expected[:,:210].tobytes()
assert raw.tobytes()==before.tobytes()
assert np.array_equal(actual[:,214],expected[:,214])
np.testing.assert_allclose(actual[:,210:],expected[:,210:],atol=2e-6,rtol=1e-5)
print(json.dumps({'core5_max_absolute_difference':float(np.max(np.abs(actual[:,210:]-expected[:,210:]))),
 'availability_exact':True,'visual_bytes_exact':True}))
'''
    result = subprocess.run([_av_python(), '-B', '-c', script, str(package),
                             str(FIXTURES / 'pcm.npy'), str(FIXTURES / 'raw.npy')],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['visual_bytes_exact'] is True


def test_actual_pyav_windows_match_frozen_decoder_at_native_48khz_before_resample(tmp_path):
    # Isolated CPU process: no training, footage, model, cloud or YOLO required.
    script = '''
from fractions import Fraction
import importlib.util,sys
import av
import numpy as np
from touchline.ml_inference.features.audio import verify_reference_decoder
from touchline.ml_inference.features.media import MediaCursor
path=sys.argv[1]
with av.open(path,'w') as output:
 video=output.add_stream('mpeg4',rate=25); video.width=video.height=16; video.pix_fmt='yuv420p'
 audio=output.add_stream('pcm_f32le',rate=48000); audio.layout='stereo'
 for i in range(200):
  v=av.VideoFrame.from_ndarray(np.full((16,16,3),i%200,np.uint8),format='rgb24')
  v.pts=i+1; v.time_base=Fraction(1,25)
  for packet in video.encode(v):output.mux(packet)
  t=(np.arange(1920)+i*1920)/48000
  wave=(.3*np.sin(t*2*np.pi*431)).astype(np.float32)
  a=av.AudioFrame.from_ndarray(np.stack([wave,-wave]),format='fltp',layout='stereo')
  a.pts=(i+1)*1920; a.time_base=Fraction(1,48000); a.sample_rate=48000
  for packet in audio.encode(a):output.mux(packet)
 for packet in video.encode(None):output.mux(packet)
 for packet in audio.encode(None):output.mux(packet)
spec=importlib.util.spec_from_file_location('frozen_audio',verify_reference_decoder())
reference=importlib.util.module_from_spec(spec); spec.loader.exec_module(reference)
cursor=MediaCursor(path,allow_local=True,audio_contract='sampleclock-v3')
for end in (5,7):
 while (cursor.next_index+.5)/15<=end:cursor.pull(end)
 actual=cursor.audio_window(end-5,end)
 expected,rate=reference.decode_audio_sampleclock(path,end-5,end,cursor._video_origin)
 assert rate==16000 and actual.shape==(80000,2)
 assert np.array_equal(np.isfinite(actual),np.isfinite(expected))
 np.testing.assert_allclose(actual,expected,atol=2e-6,rtol=1e-5,equal_nan=True)
 assert np.nanmean(actual**2)>.01
cursor.close()
print('native48k_stereo_windows_5_7_exact')
'''
    result = subprocess.run([_av_python(), '-B', '-c', script, str(tmp_path / 'native.mkv')],
                            capture_output=True, text=True, timeout=60, check=True)
    assert 'native48k_stereo_windows_5_7_exact' in result.stdout
