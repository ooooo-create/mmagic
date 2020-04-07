import os.path as osp

import cv2
import mmcv
import numpy as np

from ..registry import PIPELINES


@PIPELINES.register_module
class MergeFgAndBg(object):
    """Composite foreground image and background image with alpha.

    Required keys are "alpha", "fg" and "bg", added key is "merged".
    """

    def __call__(self, results):
        alpha = results['alpha'][..., None].astype(np.float32) / 255.
        fg = results['fg']
        bg = results['bg']
        merged = fg * alpha + (1. - alpha) * bg
        results['merged'] = merged
        return results


@PIPELINES.register_module
class GenerateTrimap(object):
    """Using random erode/dilate to generate trimap from alpha matte.

    Required key is "alpha", added key is "trimap".

    Args:
        kernel_size (int | tuple[int]): the range of random kernel_size of
            erode/dilate; int indicates a fixed kernel_size.
        iterations (int | tuple[int]): the range of random iterations of
            erode/dilate; int indicates a fixed iterations.
        symmetric (bool): wether use the same kernel_size and iterations for
            both erode and dilate.
    """

    def __init__(self, kernel_size, iterations=1, symmetric=False):
        if isinstance(kernel_size, int):
            min_kernel, max_kernel = kernel_size, kernel_size + 1
        else:
            min_kernel, max_kernel = kernel_size

        if isinstance(iterations, int):
            self.min_iteration, self.max_iteration = iterations, iterations + 1
        else:
            self.min_iteration, self.max_iteration = iterations

        self.kernels = [
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
            for size in range(min_kernel, max_kernel)
        ]
        self.symmetric = symmetric

    def __call__(self, results):
        alpha = results['alpha']

        kernel_num = len(self.kernels)
        erode_ksize_idx = np.random.randint(kernel_num)
        erode_iter = np.random.randint(self.min_iteration, self.max_iteration)
        if self.symmetric:
            dilate_ksize_idx = erode_ksize_idx
            dilate_iter = erode_iter
        else:
            dilate_ksize_idx = np.random.randint(kernel_num)
            dilate_iter = np.random.randint(self.min_iteration,
                                            self.max_iteration)

        eroded = cv2.erode(
            alpha, self.kernels[erode_ksize_idx], iterations=erode_iter)
        dilated = cv2.dilate(
            alpha, self.kernels[dilate_ksize_idx], iterations=dilate_iter)

        trimap = np.zeros_like(alpha)
        trimap.fill(128)
        trimap[eroded >= 255] = 255
        trimap[dilated <= 0] = 0
        results['trimap'] = trimap.astype(np.float32)
        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += (
            f'(kernels={self.kernels}, min_iteration={self.min_iteration}, '
            f'max_iteration={self.max_iteration}, symmetric={self.symmetric})')
        return repr_str


@PIPELINES.register_module
class CompositeFg(object):
    """Composite foreground with a random foreground.

    This class composites the current training sample with additional data
    randomly (could be from the same dataset). With probability 0.5, the sample
    will be composited with a random sample from the specified directory.
    The composition is performed as:

    .. math::
        fg_{new} = \alpha_1 * fg_1 + (1 - \alpha_1) * fg_2
        alpha_{new} = 1 - (1 - \alpha_1) * (1 - \alpha_2)

    where :math:`(fg_1, \alpha_1)` is from the current sample and
    :math:`(fg_2, \alpha_2)` is the randomly loaded sample. With the above
    composition, :math:`alpha_{new}` is still in `[0, 1]`.

    Required keys are "fg", "alpha", "img_shape" and "alpha_norm_cfg", added or
    modified keys are "alpha" and "fg". "alpha" should be normalized.

    Args:
        fg_dir (str): Path of directory to load foreground images from.
        alpha_dir (str): Path of directory to load alpha mattes from.
        fg_ext (str): File extension of foreground image.
        alpha_ext (str): File extension of alpha image.
        interpolation (str): Interpolation method to resize the randomly loaded
            images.
    """

    def __init__(self,
                 fg_dir,
                 alpha_dir,
                 fg_ext='png',
                 alpha_ext='png',
                 interpolation='nearest'):
        self.fg_dir = fg_dir
        self.alpha_dir = alpha_dir
        self.fg_ext = fg_ext
        self.alpha_ext = alpha_ext
        self.interpolation = interpolation

        self.stem_list = self._get_stem_list(fg_dir, self.fg_ext)

    def __call__(self, results):
        fg = results['fg']
        alpha = results['alpha'].astype(np.float32) / 255.
        h, w = results['img_shape']

        # randomly select fg
        if np.random.rand() < 0.5:
            idx = np.random.randint(len(self.stem_list))
            stem = self.stem_list[idx]
            fg2 = mmcv.imread(osp.join(self.fg_dir, stem + '.' + self.fg_ext))
            alpha2 = mmcv.imread(
                osp.join(self.alpha_dir, stem + '.' + self.alpha_ext),
                'grayscale')
            alpha2 = alpha2.astype(np.float32) / 255.

            fg2 = mmcv.imresize(fg2, (w, h), interpolation=self.interpolation)
            alpha2 = mmcv.imresize(
                alpha2, (w, h), interpolation=self.interpolation)

            # the overlap of two 50% transparency will be 75%
            alpha_tmp = 1 - (1 - alpha) * (1 - alpha2)
            # if the result alpha is all-one, then we avoid composition
            if np.any(alpha_tmp < 1):
                # composite fg with fg2
                fg = fg.astype(np.float32) * alpha[..., None] \
                     + fg2.astype(np.float32) * (1 - alpha[..., None])
                alpha = alpha_tmp
                fg.astype(np.uint8)

        results['fg'] = fg
        results['alpha'] = (alpha * 255).astype(np.uint8)
        results['img_shape'] = alpha.shape
        return results

    @staticmethod
    def _get_stem_list(dir_name, ext):
        name_list = mmcv.scandir(dir_name, ext)
        stem_list = list()
        for name in name_list:
            stem, _ = osp.splitext(name)
            stem_list.append(stem)
        return stem_list

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += (f"(fg_dir='{self.fg_dir}', alpha_dir='{self.alpha_dir}', "
                     f"fg_ext='{self.fg_ext}', alpha_ext='{self.alpha_ext}', "
                     f"interpolation='{self.interpolation}')")
        return repr_str