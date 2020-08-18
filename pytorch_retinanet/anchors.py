# Modified From : https://github.com/facebookresearch/detectron2/blob/master/detectron2/modeling/anchor_generator.py

import math
from typing import *

import torch
from torch import device, nn
from torch.functional import Tensor


from . import config as cfg


def ifnone(a: Any, b: Any) -> Any:
    '''`a` if `a` is not None, otherwise `b`'''
    if a is not None:
        return a
    else:
        return b


class BufferList(nn.Module):
    """
    Similar to nn.ParameterList, but for buffers
    """

    def __init__(self, buffers):
        super(BufferList, self).__init__()
        for i, buffer in enumerate(buffers):
            self.register_buffer(str(i), buffer)

    def __len__(self):
        return len(self._buffers)

    def __iter__(self):
        return iter(self._buffers.values())


def _broadcast_params(params, num_features, name) -> List[List[float]]:
    """
    If one size (or aspect ratio) is specified and there are multiple feature
    maps, we "broadcast" anchors of that single size (or aspect ratio)
    over all feature maps.
    If params is list[float], or list[list[float]] with len(params) == 1, repeat
    it num_features time.
    Returns:
        list[list[float]]: param for each feature
    """
    assert isinstance(
        params, (list, tuple)
    ), f"{name} in anchor generator has to be a list! Got {params}."
    assert len(params), f"{name} in anchor generator cannot be empty!"
    if not isinstance(params[0], (list, tuple)):  # list[float]
        return [params] * num_features
    if len(params) == 1:
        return list(params) * num_features
    assert len(params) == num_features, (
        f"Got {name} of length {len(params)} in anchor generator, "
        f"but the number of input features is {num_features}!"
    )
    return params


class AnchorGenerator(nn.Module):
    """
    Module the Generates anchors for given set of `feature maps`.

    Args:
        sizes (List[float]): is the list of anchor sizes (i.e., sqrt of anchor area)
                to use for the i-th feature map. For each area in `sizes` anchors with
                different `aspect ratios` are generated by the anchor generator.
        aspect_ratios (List[float]): list of aspect ratios (i.e., height/width) to use for anchors.
        strides (List[int]): stride of each input feature.
        offset (float): Relative offset between the center of the first anchor and the top-left
                corner of the image. Value has to be in [0, 1).
        device : torch.device
    """

    def __init__(self,
                 sizes: List[float] = cfg.ANCHOR_SIZES,
                 aspect_ratios: List[float] = cfg.ANCHOR_ASPECT_RATIOS,
                 strides: List[int] = cfg.ANCHOR_STRIDES,
                 offset: float = cfg.ANCHOR_OFFSET,
                 device: torch.device = torch.device('cpu')) -> None:

        super().__init__()
        # Anchors have areas of 32**2 to 512**2 on pyramid levels P3 to P7
        # at each pyramid level we use anchors at three aspect ratios {1:2; 1:1, 2:1}
        # at each anchor level we add anchors of sizes {2**0, 2**(1/3), 2**(2/3)} of the original set of 3 anchors
        # In total there are A=9 anchors at each feature map for each pixel
        

        self.strides = strides
        self.num_features = len(strides)

        self.sizes = _broadcast_params(sizes, self.num_features, 'sizes')
        self.aspect_ratios = _broadcast_params(
            aspect_ratios, self.num_features, 'aspect_ratios')

        self.cell_anchors = self._calculate_anchors(
            self.sizes, self.aspect_ratios)

        self.offset = offset
        self._device = device

    @property
    def device(self) -> torch.device:
        return self._device

    def _calculate_anchors(self, sizes, aspect_ratios) -> List[Tensor]:
        # Generate anchors of `size` (for size in sizes) of `ratio` (for ratio in aspect_ratios)
        cell_anchors = ([self.generate_cell_anchors(s, a)
                         .float() for s, a in zip(sizes, aspect_ratios)])
        return BufferList(cell_anchors)

    @staticmethod
    def _compute_grid_offsets(size: List[int], stride: int, offset: float, device: torch.device):
        """Compute grid offsets of `size` with `stride`"""
        H, W = size

        shifts_x = torch.arange(
            offset * stride, W * stride, step=stride, dtype=torch.float32, device=device)
        shifts_y = torch.arange(
            offset * stride, H * stride, step=stride, dtype=torch.float32, device=device)

        shifts_y, shifts_x = torch.meshgrid(shifts_y, shifts_x)

        shifts_x, shifts_y = shifts_x.reshape(-1), shifts_y.reshape(-1)

        return shifts_x, shifts_y

    @property
    def num_cell_anchors(self):
        return self.num_anchors

    @property
    def num_anchors(self) -> List[int]:
        """
        Returns : List[int] : Each int is the number of anchors at every pixel
                              location in the feature map.
                              For example, if at every pixel we use anchors of 3 aspect
                              ratios and 3 sizes, the number of anchors is 9.
        """
        return [len(cell_anchors) for cell_anchors in self.cell_anchors]

    def generate_cell_anchors(self, sizes, aspect_ratios) -> Tensor:
        """
        Generates a Tensor storing cannonical anchor boxes, where all
        anchor boxes are of different sizes & aspect ratios centered at (0,0).
        We can later build the set of anchors for a full feature map by
        shifting and tiling these tensors.

        Args:
            sizes (tuple[float]):
            aspect_ratios (tuple[float]]):

        Returns:
            Tensor of shape (len(sizes)*len(aspect_ratios), 4) storing anchor boxes in XYXY format
        """
        # instantiate empty anchor list to store anchors
        anchors = []
        for size in sizes:
            area = size ** 2.0
            for aspect_ratio in aspect_ratios:
                w = math.sqrt(area / aspect_ratio)
                h = aspect_ratio * w
                x0, y0, x1, y1 = -w / 2.0, -h / 2.0, w / 2.0, h / 2.0
                anchors.append([x0, y0, x1, y1])
        return torch.tensor(anchors)

    def grid_anchors(self, grid_sizes: List[List[int]]) -> List[Tensor]:
        """
        Returns : list[Tensor] : #feature_map tensors, each is (#locations x #cell_anchors) x 4
        """
        anchors = []
        buffers: List[torch.Tensor] = [x[1]
                                       for x in self.cell_anchors.named_buffers()]

        for size, stride, base_anchors in zip(grid_sizes, self.strides, buffers):
            # Compute grid offsets from `size` and `stride`
            shift_x, shift_y = self._compute_grid_offsets(
                size, stride, self.offset, device=self.device)
            shifts = torch.stack((shift_x, shift_y, shift_x, shift_y), dim=1)
            # shift base anchors to get the set of anchors for a full feature map
            anchors.append(
                (shifts.view(-1, 1, 4) + base_anchors.view(1, -1, 4)).reshape(-1, 4))
        return anchors

    def forward(self, features: List[Tensor]) -> List[Tensor]:
        """
        Generate `Anchors` for each `Feature Map`.

        Args:
          1. features (list[Tensor]): list of backbone feature maps on which to generate anchors.

        Returns:
          list[Tensor]: a list of Tensors containing all the anchors for each feature map
                        (i.e. the cell anchors repeated over all locations in the feature map).
                        The number of anchors of each feature map is Hi x Wi x num_cell_anchors,
                        where Hi, Wi are Height & Width of the Feature Map respectively.
        """
        grid_sizes = [feature_map.shape[-2:] for feature_map in features]
        anchors_over_all_feature_maps = self.grid_anchors(grid_sizes)
        anchors = [torch.cat(anchors_over_all_feature_maps, dim=0)]
        return anchors
