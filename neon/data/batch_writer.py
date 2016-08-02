#!/usr/bin/env python
# ----------------------------------------------------------------------------
# Copyright 2015-2016 Nervana Systems Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ----------------------------------------------------------------------------
from builtins import str, zip
import logging
from glob import glob
import numpy as np
import os
import tarfile
import ctypes as ct
import tqdm
import struct
from collections import defaultdict
import multiprocessing
import subprocess
import shlex
from itertools import imap, izip, repeat

import PIL
from PIL import Image

from neon import logger as neon_logger

logger = logging.getLogger(__name__)


class Ingester(object):
    """
    Parent class for ingest objects for taking a set of input images and a manifest
    and transformed data for use with the DataLoader data provider. Subclasses include
    IngestI1k, IngestCIFAR10 and IngestCSV.

    Arguments:
        out_dir (str): Directory to output the macrobatches
        input_dir (str): Directory to find the images.  For general batch writer, directory
                         should be organized in subdirectories with each subdirectory
                         containing a different category of images.  For imagenet batch writer,
                         directory should contain the ILSVRC provided tar files.
        validation_pct (float, optional):  Percentage between 0 and 1 indicating what percentage
                                           of the data to hold out for validation. Default is 0.2.
        class_samples_max (int, optional): Maximum number of images to include for each class
                                           from the input image directories.  Default is None,
                                           which indicates no maximum.
        file_pattern (str, optional): file suffix to use for globbing from the input_dir.
                                      Default is '.jpg'
    """

    def __init__(self, out_dir, input_dir, target_size=256, validation_pct=0.2,
                 class_samples_max=None, file_pattern='*.jpg'):

        np.random.seed(0)
        self.out_dir = os.path.expanduser(out_dir)
        self.input_dir = os.path.expanduser(input_dir) if input_dir is not None else None
        self.file_pattern = file_pattern
        self.class_samples_max = class_samples_max
        self.validation_pct = validation_pct
        self.train_file = os.path.join(self.out_dir, 'train_file.csv')
        self.val_file = os.path.join(self.out_dir, 'val_file.csv')
        self.batch_prefix = 'macrobatch_'
        self.target_size = target_size
        self._target_filenames = {}

        self.post_init()

    def post_init(self):
        """
        Post initialization steps.
        """
        pass

    def _generate_target_file(self, target):
        """
        Generate a file whose only contents are the binary representation
        of `target`.  Returns the filename of this generated file.
        """
        assert(isinstance(target, int))
        filename = os.path.join(self.out_dir, str(target) + '.txt')
        with open(filename, 'wb') as f:
            f.write(str(target))
        return filename

    def _target_filename(self, target):
        """
        Return a filename of a file containing a binary representation of
        target.  If no such file exists, make one.
        """
        target_filename = self._target_filenames.get(target)
        if target_filename is None:
            target_filename = self._generate_target_file(target)
            self._target_filenames[target] = target_filename

        return target_filename

    def training_validation_pairs(self):
        """
        Returns {
            'train': [(filename, label_index), ...],
            'valid': [(filename, label_index), ...],
        }
        """
        # Get the labels as the subdirs
        subdirs = glob(os.path.join(self.input_dir, '*'))
        self.label_names = sorted([os.path.basename(x) for x in subdirs])

        indexes = list(range(len(self.label_names)))
        self.label_dict = {k: v for k, v in zip(self.label_names, indexes)}

        tlines = []
        vlines = []
        for subdir in subdirs:
            subdir_label = self.label_dict[os.path.basename(subdir)]
            files = glob(os.path.join(subdir, self.file_pattern))
            if self.class_samples_max is not None:
                files = files[:self.class_samples_max]
            lines = [(filename, subdir_label) for filename in files]
            v_idx = int(self.validation_pct * len(lines))
            tlines += lines[v_idx:]
            vlines += lines[:v_idx]
        np.random.shuffle(tlines)

        return {
            'train': tlines,
            'valid': vlines,
        }

    def write_csv_files(self, training_validation_pairs):
        """
        Write CSV files to disk.
        """
        if not os.path.exists(self.out_dir):
            os.makedirs(self.out_dir)

        for setn, pairs in training_validation_pairs.iteritems():
            filename = getattr(self, setn + '_file')
            with open(filename, 'wb') as f:
                for filename, target in pairs:
                    f.write('{},{}\n'.format(
                        filename, self._target_filename(int(target))
                    ))

    def run(self):
        """
        perform ingest
        """
        self.write_csv_files(self.training_validation_pairs())


class ImageIngester(Ingester):
    def __init__(self, out_dir, input_dir, target_size=None, **kwargs):
        """
        target_size (int, optional): Size to which to scale DOWN the shortest side of the
                                     input image.  For example, if an image is 200 x 300, and
                                     target_size is 100, then the image will be scaled to
                                     100 x 150.  However if the input image is 80 x 80, then
                                     the image will not be resized.
                                     If target_size is 0, no resizing is done.
                                     Default is 256.
        """
        super(ImageIngester, self).__init__(out_dir, input_dir, target_size, **kwargs)

    def resize(self, image_data):
        """
        resize image in memory and return the new resized image data
        """
        stdoutdata, stderrdata = subprocess.Popen(shlex.split((
            'convert jpg:- '
            '-resize \"{target_size}x{target_size}^>\" '
            '-interpolate Catrom jpg:-'
        ).format(target_size=self.target_size)),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE).communicate(image_data)
        return stdoutdata

def process_i1k_tar_subpath(args):
    """
    Process a single subpath in a I1K tar. By process:
        optionally untar recursive tars (only on 'train')
        resize/copy images
    Returns a list of [(fname, label), ...]
    """
    target_size, toptar, img_dir, setn, label_dict, subpath = args
    name_slice = slice(None, 9) if setn == 'train' else slice(15, -5)
    label = label_dict[subpath.name[name_slice]]
    outpath = os.path.join(img_dir, str(label))
    if setn == 'train':
        tf = tarfile.open(toptar)
        subtar = tarfile.open(fileobj=tf.extractfile(subpath))
        file_list = subtar.getmembers()
        return process_files_in_tar(target_size, label, subtar, file_list, outpath)
    elif setn == 'val':
        tf = tarfile.open(toptar)
        file_list = [subpath]
        return process_files_in_tar(target_size, label, tf, file_list, outpath)

def process_files_in_tar(target_size, label, tar_handle, file_list, outpath):
    pair_list = []
    if not os.path.exists(outpath):
        os.makedirs(outpath)
    for fobj in file_list:
        fname = os.path.join(outpath, fobj.name)
        if not os.path.exists(fname):
            transform_and_save(target_size, tar_handle.extractfile(fobj), fname)
        pair_list.append((fname, label))
    return pair_list

def transform_and_save(target_size, img_handle, output_filename):
    """
    Takes a file handle to an image, optionally transforms it and then writes it out to output_filename
    """
    img = Image.open(img_handle)
    width, height = img.size

    # Take the smaller image dimension down to target_size
    # while retaining aspect_ration. Otherwise leave it alone
    if width < height:
        if width > target_size:
            scale_factor = float(target_size) / width
            width = target_size
            height = int(height*scale_factor)
            img = img.resize((width, height), resample=PIL.Image.LANCZOS)
    else:
        if height > target_size:
            scale_factor = float(target_size) / height
            height = target_size
            width = int(width*scale_factor)
    if img.size[0] != width or img.size[1] != height:
        img = img.resize((width, height), resample=PIL.Image.LANCZOS)
        img.save(output_filename, quality=95)
    else:
        # Avoid recompression by saving file out directly without
        # transformation
        with open(output_filename, 'wb') as out_handle:
            out_handle.write(img_handle.read())


class IngestI1K(ImageIngester):
    def post_init(self):
        self.check_files_exist()
        self.extract_labels()

    def check_files_exist(self):
        self.train_tar = os.path.join(self.input_dir, 'ILSVRC2012_img_train.tar')
        self.val_tar = os.path.join(self.input_dir, 'ILSVRC2012_img_val.tar')
        self.devkit = os.path.join(self.input_dir, 'ILSVRC2012_devkit_t12.tar.gz')

        for filename in (self.train_tar, self.val_tar, self.devkit):
            if not os.path.exists(filename):
                raise IOError((
                    "{filename} not found. Please ensure you have ImageNet "
                    "downloaded. More info here: "
                    "http://www.image-net.org/download-imageurls"
                ).format(filename=filename))

    def extract_labels(self):
        import zlib
        import re
        with tarfile.open(self.devkit, "r:gz") as tf:
            synsetfile = 'ILSVRC2012_devkit_t12/data/meta.mat'
            valfile = 'ILSVRC2012_devkit_t12/data/ILSVRC2012_validation_ground_truth.txt'

            # get the synset mapping by hacking around matlab's terrible compressed format
            meta_buff = tf.extractfile(synsetfile).read()
            decomp = zlib.decompressobj()
            self.synsets = re.findall(re.compile('n\d+'), decomp.decompress(meta_buff[136:]))
            self.train_labels = {s: i for i, s in enumerate(self.synsets)}

            # get the ground truth validation labels and offset to zero
            self.val_labels = {"%08d" % (i + 1): int(x) - 1 for i, x in
                               enumerate(tf.extractfile(valfile))}

    def transform_and_save(self, tar_object, image_object, output_filename):
        """
        Extracts image_object out of tar_object, transforms it and then writes it out to output_filename
        """
        with open(output_filename, 'wb') as jf:
            jf.write(self.resize(tar_object.extractfile(image_object).read()))

    def training_validation_pairs(self, overwrite=False):
        """
        untar imagenet tar files into directories that indicate their label.

        returns {
            'train': [(filename, label), ...],
            'valid': [(filename, label), ...],
        }
        """
        pairs = {}
        for setn in ('train', 'val'):
            pairs[setn] = []
            img_dir = os.path.join(self.out_dir, setn)

            neon_logger.display("Extracting %s files" % (setn))
            root_tf_path = getattr(self, setn + '_tar')
            label_dict = getattr(self, setn + '_labels')

            try:
                root_tf = tarfile.open(root_tf_path)
            except tarfile.ReadError as e:
                raise ValueError('ReadError opening {}: {}'.format(root_tf_path, e))

            subpaths = root_tf.getmembers()
            arg_iterator = izip(repeat(self.target_size), repeat(root_tf_path), repeat(img_dir), repeat(setn), repeat(label_dict), subpaths)
            pool = multiprocessing.Pool()
            for pair_list in tqdm.tqdm(pool.imap_unordered(process_i1k_tar_subpath, arg_iterator), total=len(subpaths)):
                pairs[setn].extend(pair_list)
            pool.close()
            pool.join()

            root_tf.close()
        return pairs


class BatchWriterCSV(ImageIngester):

    def post_init(self):
        self.imgs, self.labels = dict(), dict()
        # check that the needed csv files exist
        for setn in ('train', 'val'):
            infile = os.path.join(self.input_dir, setn + '_file.csv.gz')
            if not os.path.exists(infile):
                raise IOError(infile + " not found.  This needs to be created prior to running"
                              "BatchWriter with CSV option")
            self.imgs[setn], self.labels[setn] = self.parse_file_list(infile)

        self.validation_pct = None

    def parse_file_list(self, infile):
        lines = np.loadtxt(infile, delimiter=',', dtype={'names': ('fname', 'l_id'),
                                                         'formats': (object, 'i4')})
        imfiles = [l[0] if l[0][0] == '/' else os.path.join(self.input_dir, l[0]) for l in lines]
        labels = {'l_id': [l[1] for l in lines]}
        self.nclass = {'l_id': (max(labels['l_id']) + 1)}
        return imfiles, labels

    def run(self):
        if not os.path.exists(self.out_dir):
            os.makedirs(self.out_dir)
        neon_logger.display("Writing train macrobatches")
        self.write_batches(self.train_start, self.labels['train'], self.imgs['train'])
        neon_logger.display("Writing validation macrobatches")
        self.write_batches(self.val_start, self.labels['val'], self.imgs['val'])
        self.save_meta()


class BatchWriterCIFAR10(IngestI1K):

    def post_init(self):
        self.pad_size = ((self.target_size - 32) // 2) if self.target_size > 32 else 0
        self.pad_width = ((0, 0), (self.pad_size, self.pad_size), (self.pad_size, self.pad_size))


    def extract_images(self, overwrite=False):
        from neon.data import load_cifar10
        dataset = dict()
        cifar10 = CIFAR10(path=self.out_dir, normalize=False)
        dataset['train'], dataset['val'], _ = cifar10.load_data()

        self.records = dict(train=[], val=[])

        for setn in ('train', 'val'):
            data, labels = dataset[setn]

            img_dir = os.path.join(self.out_dir, setn)
            ulabels = np.unique(labels)
            for ulabel in ulabels:
                subdir = os.path.join(img_dir, str(ulabel))
                label_file = os.path.join(subdir, str(ulabel) + '.txt')
                with open(label_file, 'w') as f:
                    f.write("%d" % ulabel)
                if not os.path.exists(subdir):
                    os.makedirs(subdir)

            for idx in tqdm.tqdm(range(data.shape[0])):
                im = np.pad(data[idx].reshape((3, 32, 32)), self.pad_width, mode='mean')
                im = np.uint8(np.transpose(im, axes=[1, 2, 0]).copy())
                im = Image.fromarray(im)
                path = os.path.join(img_dir, str(labels[idx][0]), str(idx) + '.png')
                im.save(path, format='PNG')

            if setn == 'train':
                self.pixel_mean = list(data.mean(axis=0).reshape(3, -1).mean(axis=1))
                self.pixel_mean.reverse()  # We will see this in BGR order b/c of opencv

    def run(self):
        self.extract_images()

if __name__ == "__main__":
    from neon.util.argparser import NeonArgparser
    parser = NeonArgparser(__doc__)
    parser.add_argument('--set_type', help='(i1k|cifar10|directory|csv)', required=True,
                        choices=['i1k', 'cifar10', 'directory', 'csv'])
    parser.add_argument('--input_dir', help='Directory to find images', default=None)
    parser.add_argument('--target_size', type=int, default=0,
                        help='Size in pixels to scale shortest side DOWN to (0 means no scaling)')
    parser.add_argument('--file_pattern', default='*.jpg', help='Image extension to include in'
                        'directory crawl')
    args = parser.parse_args()

    logger = logging.getLogger(__name__)

    if args.set_type == 'i1k':
        args.target_size = 256  # (maybe 512 for Simonyan's methodology?)
        bw = IngestI1K(out_dir=args.data_dir, input_dir=args.input_dir,
                            target_size=args.target_size,
                            file_pattern="*.JPEG")
    elif args.set_type == 'cifar10':
        bw = BatchWriterCIFAR10(out_dir=args.data_dir, input_dir=args.input_dir,
                                target_size=args.target_size,
                                file_pattern="*.png")
    elif args.set_type == 'csv':
        bw = BatchWriterCSV(out_dir=args.data_dir, input_dir=args.input_dir,
                            target_size=args.target_size)
    else:
        bw = ImageIngester(out_dir=args.data_dir, input_dir=args.input_dir,
                           target_size=args.target_size,
                           file_pattern=args.file_pattern)

    bw.run()
