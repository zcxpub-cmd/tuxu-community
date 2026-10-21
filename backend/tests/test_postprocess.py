from pathlib import Path
import hashlib
import pytest
from PIL import Image
from backend.app.postprocess import normalize_image_size, process_image
from concurrent.futures import ThreadPoolExecutor
import threading


@pytest.mark.parametrize("size,expected", [((800,600),(2048,1536)), ((600,800),(1536,2048)),
                                          ((4000,3000),(4000,3000)), ((1,100),(20,2048))])
def test_minimum_long_edge_without_shrinking(size, expected):
    assert normalize_image_size(*size) == expected


@pytest.mark.parametrize("args", [(0,100),(-1,100),(True,100),(2.5,100),
                                   (10000,10000),(100,100,5000)])
def test_pixel_and_input_caps(args):
    with pytest.raises(ValueError):normalize_image_size(*args)


def test_processing_preserves_source_and_records_real_dimensions(tmp_path):
    src=tmp_path/'original.png';out=tmp_path/'processed.png'
    Image.new('RGB',(1024,768),'#697d63').save(src)
    before=hashlib.sha256(src.read_bytes()).hexdigest()
    result=process_image(src,out)
    assert (result['width'],result['height']) == (2048,1536)
    assert Image.open(out).size == (2048,1536)
    assert hashlib.sha256(src.read_bytes()).hexdigest()==before
    assert '非AI超分' in result['algorithm']
    with pytest.raises(ValueError):process_image(src,src)


def test_simultaneous_same_output_has_unique_temporary_files(tmp_path):
    src, output = tmp_path / 'original.png', tmp_path / 'processed.png'
    Image.new('RGB', (1024, 768), '#428377').save(src)
    original_hash = hashlib.sha256(src.read_bytes()).hexdigest()
    barrier = threading.Barrier(2)
    def run():
        barrier.wait()
        return process_image(src, output, denoise=True, upscale=2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run(), range(2)))
    assert all(r['sha256'] == hashlib.sha256(output.read_bytes()).hexdigest() for r in results)
    assert all((r['width'], r['height']) == Image.open(output).size for r in results)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == original_hash
    assert not list(tmp_path.glob('*.tmp'))


def test_simultaneous_distinct_parameters_preserve_each_real_file(tmp_path):
    src = tmp_path / 'original.png'
    image = Image.new('RGB', (1024, 600))
    image.putdata([(x % 256, (x * 13) % 256, (x * 7) % 256) for x in range(1024 * 600)])
    image.save(src)
    original_hash = hashlib.sha256(src.read_bytes()).hexdigest()
    parameters = [(False, 2), (True, 3)]
    barrier = threading.Barrier(2)
    def run(index):
        denoise, scale = parameters[index]
        path = tmp_path / f'processed-version-{index}.png'
        barrier.wait()
        result = process_image(src, path, denoise=denoise, upscale=scale)
        return path, result
    with ThreadPoolExecutor(max_workers=2) as executor:
        outputs = list(executor.map(run, range(2)))
    for path, result in outputs:
        assert result['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert (result['width'], result['height']) == Image.open(path).size
    assert outputs[0][1]['sha256'] != outputs[1][1]['sha256']
    assert hashlib.sha256(src.read_bytes()).hexdigest() == original_hash
    assert not list(tmp_path.glob('*.tmp'))
