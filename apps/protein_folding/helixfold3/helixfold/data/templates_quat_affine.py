#   Copyright (c) 2024 PaddleHelix Authors. All Rights Reserved.
#
# Licensed under Creative Commons Attribution-NonCommercial-ShareAlike 4.0
# International License (the "License");  you may not use this file  except
# in compliance with the License. You may obtain a copy of the License at
#
#     http://creativecommons.org/licenses/by-nc-sa/4.0/
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Quaternion geometry modules.

This introduces a representation of coordinate frames that is based around a
‘QuatAffine’ object. This object describes an array of coordinate frames.
It consists of vectors corresponding to the
origin of the frames as well as orientations which are stored in two
ways, as unit quaternions as well as a rotation matrices.
The rotation matrices are derived from the unit quaternions and the two are kept
in sync.
For an explanation of the relation between unit quaternions and rotations see
https://en.wikipedia.org/wiki/Quaternions_and_spatial_rotation

This representation is used in the model for the backbone frames.

One important thing to note here, is that while we update both representations
the jit compiler is going to ensure that only the parts that are
actually used are executed.
"""


import numpy as np
from typing import Tuple


QUAT_TO_ROT = np.zeros((4, 4, 3, 3), dtype=np.float32)

QUAT_TO_ROT[0, 0] = [[ 1, 0, 0], [ 0, 1, 0], [ 0, 0, 1]]  # rr
QUAT_TO_ROT[1, 1] = [[ 1, 0, 0], [ 0,-1, 0], [ 0, 0,-1]]  # ii
QUAT_TO_ROT[2, 2] = [[-1, 0, 0], [ 0, 1, 0], [ 0, 0,-1]]  # jj
QUAT_TO_ROT[3, 3] = [[-1, 0, 0], [ 0,-1, 0], [ 0, 0, 1]]  # kk

QUAT_TO_ROT[1, 2] = [[ 0, 2, 0], [ 2, 0, 0], [ 0, 0, 0]]  # ij
QUAT_TO_ROT[1, 3] = [[ 0, 0, 2], [ 0, 0, 0], [ 2, 0, 0]]  # ik
QUAT_TO_ROT[2, 3] = [[ 0, 0, 0], [ 0, 0, 2], [ 0, 2, 0]]  # jk

QUAT_TO_ROT[0, 1] = [[ 0, 0, 0], [ 0, 0,-2], [ 0, 2, 0]]  # ir
QUAT_TO_ROT[0, 2] = [[ 0, 0, 2], [ 0, 0, 0], [-2, 0, 0]]  # jr
QUAT_TO_ROT[0, 3] = [[ 0,-2, 0], [ 2, 0, 0], [ 0, 0, 0]]  # kr

QUAT_MULTIPLY = np.zeros((4, 4, 4), dtype=np.float32)
QUAT_MULTIPLY[:, :, 0] = [[ 1, 0, 0, 0],
                          [ 0,-1, 0, 0],
                          [ 0, 0,-1, 0],
                          [ 0, 0, 0,-1]]

QUAT_MULTIPLY[:, :, 1] = [[ 0, 1, 0, 0],
                          [ 1, 0, 0, 0],
                          [ 0, 0, 0, 1],
                          [ 0, 0,-1, 0]]

QUAT_MULTIPLY[:, :, 2] = [[ 0, 0, 1, 0],
                          [ 0, 0, 0,-1],
                          [ 1, 0, 0, 0],
                          [ 0, 1, 0, 0]]

QUAT_MULTIPLY[:, :, 3] = [[ 0, 0, 0, 1],
                          [ 0, 0, 1, 0],
                          [ 0,-1, 0, 0],
                          [ 1, 0, 0, 0]]

QUAT_MULTIPLY_BY_VEC = QUAT_MULTIPLY[:, 1:, :]


def rot_to_quat(rot):
    """Convert rotation matrix to quaternion.

    Note that this function calls self_adjoint_eig which is extremely expensive on
    the GPU. If at all possible, this function should run on the CPU.

    Args:
        rot: rotation matrix (see below for format). rotation matrix should be shape (..., 3, 3)

    Returns:
        Quaternion as (..., 4) tensor.
    """
    rot = [ [rot[..., i, j] for j in range(3)] for i in range(3)]
    [[xx, xy, xz], [yx, yy, yz], [zx, zy, zz]] = rot

    # pylint: disable=bad-whitespace
    k = [[ xx + yy + zz,      zy - yz,      xz - zx,      yx - xy,],
         [      zy - yz, xx - yy - zz,      xy + yx,      xz + zx,],
         [      xz - zx,      xy + yx, yy - xx - zz,      yz + zy,],
         [      yx - xy,      xz + zx,      yz + zy, zz - xx - yy,]]

    k = (1./3.) * np.stack([np.stack(x, axis=-1) for x in k],
                          axis=-2)

    # Get eigenvalues in non-decreasing order and associated.
    _, qs = np.linalg.eigh(k)
    return qs[..., -1]


def quat_to_rot(normalized_quat):
    """Convert a normalized quaternion to a rotation matrix. Quat (..., 4)"""

    mat = np.expand_dims(normalized_quat, [-1, -3]) # normalized_quat[..., None, :, None]
    rot_tensor = np.sum(
        np.reshape(QUAT_TO_ROT, (4, 4, 9)) *
        normalized_quat[..., :, None, None] *
        mat,
        axis=(-3, -2)) # (..., 4, 4, 9) -> (..., 9)
    t_shape = rot_tensor.shape[:-1]
    t_shape.extend([3, 3])
    rot = np.reshape(rot_tensor, t_shape)  # Unstack. (..., 3, 3)
    return rot

def quat_multiply_by_vec(quat, vec):
    """Multiply a quaternion by a pure-vector quaternion."""
    mat = np.expand_dims(vec, [-1, -3]) # vec[..., None, :, None]
    return np.sum(
        QUAT_MULTIPLY_BY_VEC *
        quat[..., :, None, None] *
        mat,
        axis=(-3, -2))

def apply_rot_to_vec(rot, vec, unstack=False):
    """Multiply rotation matrix by a vector. vec is a list.
    Returns: a list of 3 tensors of the points
    """
    if unstack:
        x, y, z = [vec[..., i] for i in range(3)]
    else:
        x, y, z = vec
    return [rot[..., 0, 0] * x + rot[..., 0, 1] * y + rot[..., 0, 2] * z,
            rot[..., 1, 0] * x + rot[..., 1, 1] * y + rot[..., 1, 2] * z,
            rot[..., 2, 0] * x + rot[..., 2, 1] * y + rot[..., 2, 2] * z]


def apply_rot_to_vec_np(rot, vec, unstack=False):
    """Multiply rotation matrix by a vector. vec is a list.
    Returns: a list of 3 tensors of the points
    """
    if unstack:
        x, y, z = [vec[..., i] for i in range(3)]
    else:
        x, y, z = vec
    return [rot[0][0] * x + rot[0][1] * y + rot[0][2] * z,
            rot[1][0] * x + rot[1][1] * y + rot[1][2] * z,
            rot[2][0] * x + rot[2][1] * y + rot[2][2] * z]


def apply_inverse_rot_to_vec(rot, vec):
    """Multiply the inverse of a rotation matrix by a vector. vec is a list.
    Returns: a list of 3 tensors of the points
    """
    # Inverse rotation is just transpose
    x, y, z = vec
    return  [rot[..., 0, 0] * x + rot[..., 1, 0] * y + rot[..., 2, 0] * z,
             rot[..., 0, 1] * x + rot[..., 1, 1] * y + rot[..., 2, 1] * z,
             rot[..., 0, 2] * x + rot[..., 1, 2] * y + rot[..., 2, 2] * z]


class QuatAffine(object):
    """Affine transformation represented by quaternion and vector."""

    def __init__(self,
        quaternion: np.ndarray,
        translation: np.ndarray,
        rotation=None, normalize=True):
        """Initialize from quaternion and translation.

        Args:
        quaternion: Rotation represented by a quaternion, to be applied
            before translation.  Must be a unit quaternion unless normalize==True.
            shape (batch, N_res, 4)
        translation: Translation represented as a vector. (batch, N_res, 3)
        rotation: Same rotation as the quaternion, represented as a (batch, N_res, 3, 3)
            tensor.  If None, rotation will be calculated from the quaternion.
        normalize: If True, l2 normalize the quaternion on input.
        """

        if quaternion is not None:
            assert quaternion.shape[-1] == 4

        if normalize and quaternion is not None:
            q_length = np.linalg.norm(quaternion, axis=-1)
            quaternion = quaternion / q_length[..., None]

        if rotation is None:
            rotation = quat_to_rot(quaternion)

        self.quaternion = quaternion
        self.rotation = rotation
        self.translation = translation

        assert rotation.shape[-1] == 3 and rotation.shape[-2] == 3
        assert translation.shape[-1] == 3

    def to_tensor(self):
        return np.concatenate([self.quaternion, self.translation], axis=-1)

    def stop_rot_gradient(self):
        """
            stop the gradient of rotations
        """
        quat = self.quaternion
        if not quat is None:
            quat = quat.detach()
        return QuatAffine(
            quaternion=quat,
            translation=self.translation,
            rotation=self.rotation.detach(),
            normalize=False)

    def scale_translation(self, position_scale):
        """Return a new quat affine with a different scale for translation."""

        return QuatAffine(self.quaternion,
                        position_scale * self.translation,
                        rotation=self.rotation, normalize=False)

    @classmethod
    def from_tensor(cls, tensor, normalize=False):
        assert tensor.shape[-1] == 7
        quaternion = tensor[..., 0:4]
        translation = tensor[..., 4:7]
        return cls(quaternion, translation, normalize=normalize)

    def pre_compose(self, update):
        """Return a new QuatAffine which applies the transformation update first.

        Args:
        update: Length-6 vector. 3-vector of x, y, and z such that the quaternion
            update is (1, x, y, z) and zero for the 3-vector is the identity
            quaternion. 3-vector for translation concatenated.

        Returns:
        New QuatAffine object.
        """
        vector_quaternion_update = update[..., 0:3]
        trans_update = [update[..., 3], update[..., 4], update[..., 5]]

        new_quaternion = (self.quaternion +
                      quat_multiply_by_vec(self.quaternion,
                                           vector_quaternion_update))

        trans_update = apply_rot_to_vec(self.rotation, trans_update)
        trans_update = np.stack(trans_update, axis=-1)
        new_translation = self.translation + trans_update

        return QuatAffine(new_quaternion, new_translation)

    def apply_to_point(self, point, extra_dims=0):
        """Apply affine to a point.

        Args:
        point: List of 3 tensors to apply affine.
            each with shape [batch_size, num_residues, num_head*num_point_qk]
        extra_dims:  Number of dimensions at the end of the transformed_point
            shape that are not present in the rotation and translation.  The most
            common use is rotation N points at once with extra_dims=1 for use in a
            network.

        Returns:
        Transformed point after applying affine.
        """
        rotation = self.rotation # [batch_size, num_residues, 3, 3]
        translation = self.translation # [batch_size, num_residues, 3]
        for _ in range(extra_dims):
            translation = np.expand_dims(translation, axis=-2)
            rotation = np.expand_dims(rotation, axis=-3)

        rot_point = apply_rot_to_vec(rotation, point)
        return [rot_point[0] + translation[..., 0],
                rot_point[1] + translation[..., 1],
                rot_point[2] + translation[..., 2]]

    def invert_point(self, transformed_point, extra_dims=0):
        """Apply inverse of transformation to a point.

        Args:
        transformed_point: List of 3 tensors to apply affine
        extra_dims:  Number of dimensions at the end of the transformed_point
            shape that are not present in the rotation and translation.  The most
            common use is rotation N points at once with extra_dims=1 for use in a
            network.

        Returns:
        Transformed point after applying affine.
        """
        rotation = self.rotation
        translation = self.translation
        for _ in range(extra_dims):
            translation = np.expand_dims(translation, axis=-2)
            rotation = np.expand_dims(rotation, axis=-3)

        rot_point = [
            transformed_point[0] - translation[..., 0],
            transformed_point[1] - translation[..., 1],
            transformed_point[2] - translation[..., 2]]

        return apply_inverse_rot_to_vec(rotation, rot_point)


######Paddle Implementation
def _multiply(a, b):
    a1 = a[..., 0, 0]
    a2 = a[..., 0, 1]
    a3 = a[..., 0, 2]
    a11 = a[..., 1, 0]
    a12 = a[..., 1, 1]
    a13 = a[..., 1, 2]
    a21 = a[..., 2, 0]
    a22 = a[..., 2, 1]
    a23 = a[..., 2, 2]
    b1 = b[..., 0, 0]
    b2 = b[..., 1, 0]
    b3 = b[..., 0, 1]
    b11 = b[..., 1, 1]
    b12 = b[..., 2, 0]
    b13 = b[..., 0, 2]
    b21 = b[..., 1, 2]
    b22 = b[..., 2, 1]
    b23 = b[..., 2, 2]
    return np.stack([
        np.stack([
        a1*b1 + a2*b2 + a3*b12,
        a1*b3 + a2*b11 + a3*b22,
        a1*b13 + a2*b21 + a3*b23], axis=-1),

        np.stack([
        a11*b1 + a12*b2 + a13*b12,
        a11*b3 + a12*b11 + a13*b22,
        a11*b13 + a12*b21 + a13*b23], axis=-1),

        np.stack([
        a21*b1 + a22*b2+ a23*b12,
        a21*b3 + a22*b11 + a23*b22,
        a21*b13 + a22*b21 + a23*b23], axis=-1)], 
        axis=-2)


def make_canonical_transform(
    n_xyz: np.ndarray,
    ca_xyz: np.ndarray,
    c_xyz: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Returns translation and rotation matrices to canonicalize residue atoms.

    Note that this method does not take care of symmetries. If you provide the
    atom positions in the non-standard way, the N atom will end up not at
    [-0.527250, 1.359329, 0.0] but instead at [-0.527250, -1.359329, 0.0]. You
    need to take care of such cases in your code.

    Args:
        n_xyz: An array of shape [batch, n_res, 3] of nitrogen xyz coordinates.
        ca_xyz: An array of shape [batch, n_res, 3] of carbon alpha xyz coordinates.
        c_xyz: An array of shape [batch, n_res, 3] of carbon xyz coordinates.

    Returns:
        A tuple (translation, rotation) where:
        translation is an array of shape [batch, n_res, 3] defining the translation.
        rotation is an array of shape [batch, n_res, 3, 3] defining the rotation.
        After applying the translation and rotation to all atoms in a residue:
        * All atoms will be shifted so that CA is at the origin,
        * All atoms will be rotated so that C is at the x-axis,
        * All atoms will be shifted so that N is in the xy plane.
    """
    assert len(n_xyz.shape) == 3, n_xyz.shape
    assert n_xyz.shape[-1] == 3, n_xyz.shape
    assert n_xyz.shape == ca_xyz.shape == c_xyz.shape, (
      n_xyz.shape, ca_xyz.shape, c_xyz.shape)
    
    # Place CA at the origin.
    translation = -ca_xyz
    n_xyz = n_xyz + translation
    c_xyz = c_xyz + translation

    # Place C on the x-axis.
    c_x, c_y, c_z = [c_xyz[..., i] for i in range(3)]
    # Rotate by angle c1 in the x-y plane (around the z-axis).
    norm = np.sqrt(c_x ** 2 + c_y ** 2 + 1e-20)
    sin_c1 = -c_y / norm
    cos_c1 = c_x / norm
    zeros = np.zeros_like(sin_c1)
    ones = np.ones_like(sin_c1)

    c1_rot_matrix = np.stack([cos_c1, -sin_c1, zeros,
                                  sin_c1,  cos_c1, zeros,
                                  zeros,    zeros,  ones], axis=-1)
    c1_rot_matrix = c1_rot_matrix.reshape(sin_c1.shape + (3,3))

    # Rotate by angle c2 in the x-z plane (around the y-axis).
    # norm = paddle.sqrt(1e-20 + c_x ** 2 + c_y ** 2 + c_z ** 2)
    norm = np.sqrt(np.sum(c_xyz ** 2, axis=-1)) + 1e-20
    sin_c2 = c_z / norm
    cos_c2 = np.sqrt(c_x ** 2 + c_y ** 2) / norm
    c2_rot_matrix = np.stack([cos_c2,  zeros, sin_c2,
                                  zeros,    ones,  zeros,
                                  -sin_c2, zeros, cos_c2], axis=-1)
    c2_rot_matrix = c2_rot_matrix.reshape(sin_c2.shape + (3,3))

    c_rot_matrix = _multiply(c2_rot_matrix, c1_rot_matrix)
    n_xyz = np.stack(apply_rot_to_vec(c_rot_matrix, n_xyz, unstack=True), axis=-1)

    # Place N in the x-y plane.
    _, n_y, n_z = [n_xyz[..., i] for i in range(3)]
    # Rotate by angle alpha in the y-z plane (around the x-axis).
    norm = np.sqrt(n_y**2 + n_z**2 + 1e-20)
    sin_n = -n_z / norm
    cos_n = n_y / norm
    n_rot_matrix = np.stack([ones,  zeros,  zeros,
                              zeros, cos_n, -sin_n,
                              zeros, sin_n,  cos_n], axis=-1)
    n_rot_matrix = n_rot_matrix.reshape(sin_n.shape + (3,3))
    # pylint: enable=bad-whitespace

    return (translation, _multiply(n_rot_matrix, c_rot_matrix))


def make_transform_from_reference(
    n_xyz: np.ndarray,
    ca_xyz: np.ndarray,
    c_xyz: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Returns rotation and translation matrices to convert from reference.

    Note that this method does not take care of symmetries. If you provide the
    atom positions in the non-standard way, the N atom will end up not at
    [-0.527250, 1.359329, 0.0] but instead at [-0.527250, -1.359329, 0.0]. You
    need to take care of such cases in your code.

    Args:
        n_xyz: An array of shape [batch, n_res, 3] of nitrogen xyz coordinates.
        ca_xyz: An array of shape [batch, n_res, 3] of carbon alpha xyz coordinates.
        c_xyz: An array of shape [batch, n_res, 3] of carbon xyz coordinates.

    Returns:
        A tuple (rotation, translation) where:
        rotation is an array of shape [batch, n_res, 3, 3] defining the rotation.
        translation is an array of shape [batch, n_res, 3] defining the translation.
        After applying the translation and rotation to the reference backbone,
        the coordinates will approximately equal to the input coordinates.

        The order of translation and rotation differs from make_canonical_transform
        because the rotation from this function should be applied before the
        translation, unlike make_canonical_transform.
    """
    translation, rotation = make_canonical_transform(n_xyz, ca_xyz, c_xyz)
    return np.transpose(rotation, (0, 1, 3, 2)), -translation

#######Numpy Implementation
def _multiply_np(a, b):
    return np.stack([
        np.array([a[0][0]*b[0][0] + a[0][1]*b[1][0] + a[0][2]*b[2][0],
                    a[0][0]*b[0][1] + a[0][1]*b[1][1] + a[0][2]*b[2][1],
                    a[0][0]*b[0][2] + a[0][1]*b[1][2] + a[0][2]*b[2][2]]),

        np.array([a[1][0]*b[0][0] + a[1][1]*b[1][0] + a[1][2]*b[2][0],
                    a[1][0]*b[0][1] + a[1][1]*b[1][1] + a[1][2]*b[2][1],
                    a[1][0]*b[0][2] + a[1][1]*b[1][2] + a[1][2]*b[2][2]]),

        np.array([a[2][0]*b[0][0] + a[2][1]*b[1][0] + a[2][2]*b[2][0],
                    a[2][0]*b[0][1] + a[2][1]*b[1][1] + a[2][2]*b[2][1],
                    a[2][0]*b[0][2] + a[2][1]*b[1][2] + a[2][2]*b[2][2]])])


def make_canonical_transform_np(
    n_xyz: np.ndarray,
    ca_xyz: np.ndarray,
    c_xyz: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Returns translation and rotation matrices to canonicalize residue atoms.

    Note that this method does not take care of symmetries. If you provide the
    atom positions in the non-standard way, the N atom will end up not at
    [-0.527250, 1.359329, 0.0] but instead at [-0.527250, -1.359329, 0.0]. You
    need to take care of such cases in your code.

    Args:
        n_xyz: An array of shape [batch, 3] of nitrogen xyz coordinates.
        ca_xyz: An array of shape [batch, 3] of carbon alpha xyz coordinates.
        c_xyz: An array of shape [batch, 3] of carbon xyz coordinates.

    Returns:
        A tuple (translation, rotation) where:
        translation is an array of shape [batch, 3] defining the translation.
        rotation is an array of shape [batch, 3, 3] defining the rotation.
        After applying the translation and rotation to all atoms in a residue:
        * All atoms will be shifted so that CA is at the origin,
        * All atoms will be rotated so that C is at the x-axis,
        * All atoms will be shifted so that N is in the xy plane.
    """
    assert len(n_xyz.shape) == 2, n_xyz.shape
    assert n_xyz.shape[-1] == 3, n_xyz.shape
    assert n_xyz.shape == ca_xyz.shape == c_xyz.shape, (n_xyz.shape, ca_xyz.shape, c_xyz.shape)

    # Place CA at the origin.
    translation = -ca_xyz
    n_xyz = n_xyz + translation
    c_xyz = c_xyz + translation

    # Place C on the x-axis.
    c_x, c_y, c_z = [c_xyz[:, i] for i in range(3)]
    # Rotate by angle c1 in the x-y plane (around the z-axis).
    sin_c1 = -c_y / np.sqrt(1e-20 + c_x**2 + c_y**2)
    cos_c1 = c_x / np.sqrt(1e-20 + c_x**2 + c_y**2)
    zeros = np.zeros_like(sin_c1)
    ones = np.ones_like(sin_c1)
    # pylint: disable=bad-whitespace
    c1_rot_matrix = np.stack([np.array([cos_c1, -sin_c1, zeros]),
                               np.array([sin_c1,  cos_c1, zeros]),
                               np.array([zeros,    zeros,  ones])])

    # Rotate by angle c2 in the x-z plane (around the y-axis).
    sin_c2 = c_z / np.sqrt(1e-20 + c_x**2 + c_y**2 + c_z**2)
    cos_c2 = np.sqrt(c_x**2 + c_y**2) / np.sqrt(
        1e-20 + c_x**2 + c_y**2 + c_z**2)
    c2_rot_matrix = np.stack([np.array([cos_c2,  zeros, sin_c2]),
                              np.array([zeros,    ones,  zeros]),
                              np.array([-sin_c2, zeros, cos_c2])])

    c_rot_matrix = _multiply_np(c2_rot_matrix, c1_rot_matrix)
    n_xyz = np.stack(apply_rot_to_vec_np(c_rot_matrix, n_xyz, unstack=True)).T

    # Place N in the x-y plane.
    _, n_y, n_z = [n_xyz[:, i] for i in range(3)]
    # Rotate by angle alpha in the y-z plane (around the x-axis).
    sin_n = -n_z / np.sqrt(1e-20 + n_y**2 + n_z**2)
    cos_n = n_y / np.sqrt(1e-20 + n_y**2 + n_z**2)
    n_rot_matrix = np.stack([np.array([ones,  zeros,  zeros]),
                              np.array([zeros, cos_n, -sin_n]),
                              np.array([zeros, sin_n,  cos_n])])

    return (translation, np.transpose(_multiply_np(n_rot_matrix, c_rot_matrix), [2, 0, 1]))
