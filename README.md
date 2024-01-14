# Gaussian_torch

try to replace the CUDA part in offical implemention with pytorch 

## how to use

This project is mainly based on official implemention of Gaussian Spaltiing , the args are all similar to official implemention.To get detailed explanation of args , you can click [it](https://github.com/graphdeco-inria/gaussian-splatting) to look detailed imformation

After your training , if you want it to look in web , you can download your output folders' .ply file and drag it to [there](https://antimatter15.com/splat/) to look it in the 3D scene .

Noticed that because of CUDA memory , we all reduce the image resolution to 0.125 by using args of "-r" "8"

Now we support 1000 iterations in a TiTanXP in 1.5 hours around , and its SSIM is 0.45, PSNR is 19.68


## reference

1. [](https://github.com/graphdeco-inria/gaussian-splatting)
2. [](https://github.com/hbb1/torch-splatting)