#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import torch
import math
from scene.gaussian_model import GaussianModel
from utils.sh_utils import eval_sh

def render(viewpoint_camera, pc : GaussianModel, white_background, scaling_modifier = 1.0, override_color = None):
    """
    Render the scene. 
    
    Background tensor (bg_color) must be on GPU!
    """
    def build_color(means3D, shs, camera): # 计算每个3D点的颜色
        rays_o = camera.camera_center
        rays_d = means3D - rays_o # 计算每个3D点到相机中心的方向向量
        color = eval_sh(pc.active_sh_degree, shs.permute(0,2,1), rays_d) # 使用eval_sh函数将球谐函数转换为每个方向的RGB颜色
        color = (color + 0.5).clip(min=0.0) # 将颜色值调整到[0, 1]范围内
        return color
    
    def renderer(camera, means2D, cov2d, color, opacity, depths,pc,white_background):
        radii = get_radius(cov2d)
        rect = get_rect(means2D, radii, width=camera.image_width, height=camera.image_height)
        pix_coord = torch.stack(torch.meshgrid(torch.arange(camera.image_width), torch.arange(camera.image_height), indexing='xy'), dim=-1).to('cuda')
        render_color = torch.ones(*pix_coord.shape[:2], 3).to('cuda')
        # render_depth = torch.zeros(*pix_coord.shape[:2], 1).to('cuda')
        # render_alpha = torch.zeros(*pix_coord.shape[:2], 1).to('cuda') # 用于存储渲染结果

        # # test code
        # loss = radii.sum()
        # loss.backward()
        TILE_SIZE = 16
        for h in range(0, camera.image_height, TILE_SIZE):
            for w in range(0, camera.image_width, TILE_SIZE):
                # check if the rectangle penetrate the tile
                over_tl = rect[0][..., 0].clip(min=w), rect[0][..., 1].clip(min=h)
                over_br = rect[1][..., 0].clip(max=w+TILE_SIZE-1), rect[1][..., 1].clip(max=h+TILE_SIZE-1) # 计算矩形区域与tile的交集的坐标
                in_mask = (over_br[0] > over_tl[0]) & (over_br[1] > over_tl[1]) # 3D gaussian in the tile 找出与当天tile有交集的3D gaussian
                
                if not in_mask.sum() > 0: # 如果没有交集则跳过
                    continue

                P = in_mask.sum() # 计算交集的个数
                tile_coord = pix_coord[h:h+TILE_SIZE, w:w+TILE_SIZE].flatten(0,-2) # 获取当前tile的坐标并将其展平


                sorted_depths, index = torch.sort(depths[in_mask]) # 按照深度排序
                sorted_means2D = means2D[in_mask][index] 
                sorted_cov2d = cov2d[in_mask][index] # P 2 2
                sorted_conic = sorted_cov2d.inverse() # inverse of variance
                sorted_opacity = opacity[in_mask][index]
                sorted_color = color[in_mask][index] # 根据排序结果获取对应的2D高斯分布的参数
                dx = (tile_coord[:,None,:] - sorted_means2D[None,:]) # B P 2 # 计算当前tile中每个像素点与2D高斯分布中心点的距离
                
                gauss_weight = torch.exp(-0.5 * (
                    dx[:, :, 0]**2 * sorted_conic[:, 0, 0] 
                    + dx[:, :, 1]**2 * sorted_conic[:, 1, 1]
                    + dx[:,:,0]*dx[:,:,1] * sorted_conic[:, 0, 1]
                    + dx[:,:,0]*dx[:,:,1] * sorted_conic[:, 1, 0])) # 计算每个像素在3D高斯分布中的权重
                
                alpha = (gauss_weight[..., None] * sorted_opacity[None]).clip(max=0.99) # B P 1，计算每个像素的透明度
                T = torch.cat([torch.ones_like(alpha[:,:1]), 1-alpha[:,1:]], dim=1).cumprod(dim=1) # 计算每个像素在每个3D高斯分布的累积透明度
                acc_alpha = (alpha * T).sum(dim=1) # 计算每个像素的累积透明度
                tile_color = (T * alpha * sorted_color[None]).sum(dim=1) + (1-acc_alpha) * (1 if white_background else 0)
                tile_depth = ((T * alpha) * sorted_depths[None,:,None]).sum(dim=1) # 计算每个像素的颜色和深度

                # 计算实际的行数和列数
                remaining_rows = min(TILE_SIZE, render_color.shape[0] - h)
                remaining_cols = min(TILE_SIZE, render_color.shape[1] - w)

                render_color[h:h+remaining_rows, w:w+remaining_cols] = tile_color.reshape(remaining_rows, remaining_cols, -1) 
                # render_depth[h:h+TILE_SIZE, w:w+TILE_SIZE] = tile_depth.reshape(TILE_SIZE, TILE_SIZE, -1)
                # render_alpha[h:h+TILE_SIZE, w:w+TILE_SIZE] = acc_alpha.reshape(TILE_SIZE, TILE_SIZE, -1) # 将计算出的结果存储到render_color, render_depth, render_alpha中

        image = render_color.permute(2,0,1) # 将渲染结果转换为pytorch的格式
                
        return {
            "render": image,
            "viewspace_points": means2D, # use to compute the grad
            "visibility_filter": radii > 0,
            "radii": radii
        }


    def forward(camera, pc,white_background): # 主要目的是将3DGaussian投影到2D图像上
        means3D = pc.get_xyz
        opacity = pc.get_opacity
        scales = pc.get_scaling
        rotations = pc.get_rotation
        shs = pc.get_features
            
        mean_ndc, mean_view, in_mask = projection_ndc(means3D, 
                viewmatrix=camera.world_view_transform, 
                projmatrix=camera.projection_matrix)
        depths = mean_view[:,2] # 提取视图空间中的深度信息
        
        color = build_color(means3D=means3D, shs=shs, camera=camera) # 计算每个3D点的颜色

        cov3d = build_covariance_3d(scales, rotations)
            
        cov2d = build_covariance_2d(
            mean3d=means3D, 
            cov3d=cov3d, 
            viewmatrix=camera.world_view_transform,
            fov_x=camera.FoVx, 
            fov_y=camera.FoVy, 
            focal_x=camera.focal_x, 
            focal_y=camera.focal_y)

        mean_coord_x = ((mean_ndc[..., 0] + 1) * camera.image_width - 1.0) * 0.5
        mean_coord_y = ((mean_ndc[..., 1] + 1) * camera.image_height - 1.0) * 0.5
        means2D = torch.stack([mean_coord_x, mean_coord_y], dim=-1) # 用OPENGL的坐标系计算2D高斯分布的p屏幕中心点坐标
        rets = renderer(
            camera = camera, 
            means2D=means2D,
            cov2d=cov2d,
            color=color,
            opacity=opacity, 
            depths=depths,
            pc=pc,
            white_background=white_background,
        )

        return rets
    ret = forward(viewpoint_camera, pc,white_background) # 将3DGaussian投影到2D图像上
    return ret

    

def inverse_sigmoid(x):
    return torch.log(x/(1-x))

def homogeneous(points): # 将3D点转为齐次坐标
    """
    homogeneous points
    :param points: [..., 3]
    """
    return torch.cat([points, torch.ones_like(points[..., :1])], dim=-1)


def build_rotation(r):
    norm = torch.sqrt(r[:,0]*r[:,0] + r[:,1]*r[:,1] + r[:,2]*r[:,2] + r[:,3]*r[:,3])

    q = r / norm[:, None]

    R = torch.zeros((q.size(0), 3, 3), device='cuda')

    r = q[:, 0]
    x = q[:, 1]
    y = q[:, 2]
    z = q[:, 3]

    R[:, 0, 0] = 1 - 2 * (y*y + z*z)
    R[:, 0, 1] = 2 * (x*y - r*z)
    R[:, 0, 2] = 2 * (x*z + r*y)
    R[:, 1, 0] = 2 * (x*y + r*z)
    R[:, 1, 1] = 1 - 2 * (x*x + z*z)
    R[:, 1, 2] = 2 * (y*z - r*x)
    R[:, 2, 0] = 2 * (x*z - r*y)
    R[:, 2, 1] = 2 * (y*z + r*x)
    R[:, 2, 2] = 1 - 2 * (x*x + y*y)
    return R



def build_scaling_rotation(s, r): # 该函数用于计算3D高斯分布的缩放和旋转矩阵
    L = torch.zeros((s.shape[0], 3, 3), dtype=torch.float, device="cuda")
    R = build_rotation(r)

    L[:,0,0] = s[:,0]
    L[:,1,1] = s[:,1]
    L[:,2,2] = s[:,2]

    L = R @ L
    return L


def strip_lowerdiag(L):
    uncertainty = torch.zeros((L.shape[0], 6), dtype=torch.float, device="cuda")
    uncertainty[:, 0] = L[:, 0, 0]
    uncertainty[:, 1] = L[:, 0, 1]
    uncertainty[:, 2] = L[:, 0, 2]
    uncertainty[:, 3] = L[:, 1, 1]
    uncertainty[:, 4] = L[:, 1, 2]
    uncertainty[:, 5] = L[:, 2, 2]
    return uncertainty


def strip_symmetric(sym):
    return strip_lowerdiag(sym)



def build_covariance_3d(s, r):
    L = build_scaling_rotation(s, r) # 首先计算3D高斯分布的缩放和旋转矩阵
    actual_covariance = L @ L.transpose(1, 2) # 计算3D高斯分布的协方差矩阵
    return actual_covariance # 返回3D高斯分布的协方差矩阵，通过协方差矩阵定义了3D高斯分布的形状和方向
    # symm = strip_symmetric(actual_covariance)
    # return symm



def build_covariance_2d(
    mean3d, cov3d, viewmatrix, fov_x, fov_y, focal_x, focal_y
):  # 该函数用于计算3D高斯分布在2D图像上的协方差矩阵
    # The following models the steps outlined by equations 29
	# and 31 in "EWA Splatting" (Zwicker et al., 2002). 
	# Additionally considers aspect / scaling of viewport.
	# Transposes used to account for row-/column-major conventions.
    tan_fovx = math.tan(fov_x * 0.5)
    tan_fovy = math.tan(fov_y * 0.5)
    t = (mean3d @ viewmatrix[:3,:3]) + viewmatrix[-1:,:3]

    # truncate the influences of gaussians far outside the frustum.
    tx = (t[..., 0] / t[..., 2]).clip(min=-tan_fovx*1.3, max=tan_fovx*1.3) * t[..., 2]
    ty = (t[..., 1] / t[..., 2]).clip(min=-tan_fovy*1.3, max=tan_fovy*1.3) * t[..., 2]
    tz = t[..., 2] # 对t进行裁剪确保其在视锥体内

    # Eq.29 locally affine transform 
    # perspective transform is not affine so we approximate with first-order taylor expansion
    # notice that we multiply by the intrinsic so that the variance is at the sceen space
    J = torch.zeros(mean3d.shape[0], 3, 3).to(mean3d)
    J[..., 0, 0] = 1 / tz * focal_x
    J[..., 0, 2] = -tx / (tz * tz) * focal_x
    J[..., 1, 1] = 1 / tz * focal_y
    J[..., 1, 2] = -ty / (tz * tz) * focal_y # 计算局部仿射变换矩阵，用泰勒展开式近似透视变换，因为透视变换不是仿射变换
    # J[..., 2, 0] = tx / t.norm(dim=-1) # discard
    # J[..., 2, 1] = ty / t.norm(dim=-1) # discard
    # J[..., 2, 2] = tz / t.norm(dim=-1) # discard
    W = viewmatrix[:3,:3].T # transpose to correct viewmatrix
    cov2d = J @ W @ cov3d @ W.T @ J.permute(0,2,1) # 通过仿射变换和透视变换将3D协方差矩阵转换为2D协方差矩阵
    
    # add low pass filter here according to E.q. 32
    filter = torch.eye(2,2).to(cov2d) * 0.3 # 可视为一个低通滤波器
    return cov2d[:, :2, :2] + filter[None]


def projection_ndc(points, viewmatrix, projmatrix): # 将3DGaussian投影到NDC空间
    points_o = homogeneous(points) # object space 将3D坐标转换为齐次坐标
    points_h = points_o @ viewmatrix @ projmatrix # screen space # RHS 将3D点从对象空间转换到屏幕空间，@表示矩阵乘法，第一个@表示将3D点转换到视图空间，第二个@表示将视图空间投影到屏幕空间 
    p_w = 1.0 / (points_h[..., -1:] + 0.000001) # 透视除法：除以齐次坐标w分量得到透视权重
    p_proj = points_h * p_w # 将屏幕空间点转换到投影空间
    p_view = points_o @ viewmatrix # 将屏幕空间点转移到视图空间
    in_mask = p_view[..., 2] >= 0.2 #创建掩码，标记哪些点在视图空间z轴位置大于0.2
    return p_proj, p_view, in_mask


@torch.no_grad()
def get_radius(cov2d): # 该函数用于计算2D高斯分布的半径
    det = cov2d[:, 0, 0] * cov2d[:,1,1] - cov2d[:, 0, 1] * cov2d[:,1,0]  # 首先计算协方差矩阵的行列式
    mid = 0.5 * (cov2d[:, 0,0] + cov2d[:,1,1]) # 计算协方差矩阵的迹的一半，即协方差矩阵的特征值的平均值
    lambda1 = mid + torch.sqrt((mid**2-det).clip(min=0.1))
    lambda2 = mid - torch.sqrt((mid**2-det).clip(min=0.1)) # 计算协方差矩阵的特征值
    return 3.0 * torch.sqrt(torch.max(lambda1, lambda2)).ceil() # 基于3倍标准差的原则计算半径

@torch.no_grad()
def get_rect(pix_coord, radii, width, height): # 该函数用于计算2D高斯分布的矩形区域
    rect_min = (pix_coord - radii[:,None])  # pix_coord是2D高斯分布的中心点，radii是2D高斯分布的半径
    rect_max = (pix_coord + radii[:,None]) # 计算矩形区域的最小和最大坐标
    rect_min[..., 0] = rect_min[..., 0].clip(0, width - 1.0)
    rect_min[..., 1] = rect_min[..., 1].clip(0, height - 1.0)
    rect_max[..., 0] = rect_max[..., 0].clip(0, width - 1.0)
    rect_max[..., 1] = rect_max[..., 1].clip(0, height - 1.0) # 限制矩形区域的x,y坐标在图像范围内
    return rect_min, rect_max