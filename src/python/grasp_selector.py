import numpy as np

from pydrake.all import (AbstractValue, Concatenate, LeafSystem, PointCloud, RigidTransform, 
                         RollPitchYaw, Sphere, Rgba, Image, ImageRgba8U, ImageDepth16U, ImageDepth32F)

from manipulation.clutter import GenerateAntipodalGraspCandidate

import helpers

import segmentation
from  PIL import Image as PILImage
from lang_sam.utils import draw_image

import torchvision.transforms.functional as Tf
import matplotlib.pyplot as plt
from matplotlib.pyplot import plot, draw, show, ion

# Takes 3 point clouds (in world coordinates) as input, and outputs and estimated pose for the items.
class GraspSelector(LeafSystem):
    def __init__(self, plant, shelf_instance, camera_body_indices, cropPointA, cropPointB, meshcat, running_as_notebook, diag, station):
        LeafSystem.__init__(self)
        model_point_cloud = AbstractValue.Make(PointCloud(0))
        cntxt31 = diag.CreateDefaultContext()
        rgb_im = station.GetOutputPort('camera{}_rgb_image'.format(1)).Eval(cntxt31).data
        self.DeclareAbstractInputPort("cloud0_W", model_point_cloud)
        self.DeclareAbstractInputPort("cloud1_W", model_point_cloud)
        self.DeclareAbstractInputPort("cloud2_W", model_point_cloud)
        self.DeclareAbstractInputPort(
            "body_poses", AbstractValue.Make([RigidTransform()]))

        port = self.DeclareAbstractOutputPort(
            "grasp_selection", lambda: AbstractValue.Make(
                (np.inf, RigidTransform())), self.SelectGrasp)
        self.DeclareAbstractInputPort("rgb1", AbstractValue.Make(Image(31,31)))
        self.DeclareAbstractInputPort("depth1", AbstractValue.Make(ImageDepth32F(31,31)))
        port.disable_caching_by_default()

        # Compute crop box.
        context = plant.CreateDefaultContext()
        X_B = RigidTransform([0,0,0])
        a = X_B.multiply(cropPointA)
        b = X_B.multiply(cropPointB)

        self.station = station
        self.diag = diag
        self.cntxt31 = diag.CreateDefaultContext()

        #self.num_classes = 7
        #self.model, self.device = segmentation.setup_model(self.num_classes)

        # cntxt31 = diag.CreateDefaultContext()
        # rgb_im = diag.GetOutputPort('camera{}_rgb_image'.format(1)).Eval(cntxt31).data
        

        # res = model([Tf.to_tensor(rgb_im[:, :, :3]).to(device)])

        # print(res[0].keys())
        # print(res[0]["masks"])
        # print(res[0]["labels"])
        # print(res[0]["scores"])

        
        if True: # corners of the crop box
            meshcat.SetObject("pick1", Sphere(0.01), rgba=Rgba(.9, .1, .1, 1))
            meshcat.SetTransform("pick1", RigidTransform(a))
            meshcat.SetObject("pick2", Sphere(0.01), rgba=Rgba(.1, .9, .1, 1))
            meshcat.SetTransform("pick2", RigidTransform(b))

        self._crop_lower = np.minimum(a,b)
        self._crop_upper = np.maximum(a,b)

        self._internal_model = helpers.make_internal_model()
        self._internal_model_context = self._internal_model.CreateDefaultContext()
        self._rng = np.random.default_rng()
        self._camera_body_indices = camera_body_indices
        self.running_as_notebook = running_as_notebook

        self.lang_sam_model = segmentation.get_lang_sam("vit_b")


        cam1 = diag.GetSubsystemByName("camera1")
        self.cam_info = cam1.depth_camera_info()
        print("CAM INFO", self.cam_info)

        cam1_context = cam1.GetMyMutableContextFromRoot(cntxt31)
        self.X_WC_Cam1 = cam1.body_pose_in_world_output_port().Eval(cam1_context)

    def project_depth_to_pC(self, depth_pixel, uv=None):
        """
        project depth pixels to points in camera frame
        using pinhole camera model
        Input:
            depth_pixels: numpy array of (nx3) or (3,)
        Output:
            pC: 3D point in camera frame, numpy array of (nx3)
        """
        # switch u,v due to python convention
        v = depth_pixel[:, 0]
        u = depth_pixel[:, 1]
        Z = depth_pixel[:, 2]
        # read camera intrinsics
        cx = self.cam_info.center_x()
        cy = self.cam_info.center_y()
        fx = self.cam_info.focal_x()
        fy = self.cam_info.focal_y()
        X = (u - cx) * Z / fx
        Y = (v - cy) * Z / fy
        pC = np.c_[X, Y, Z]
        return pC

    def SelectGrasp(self, context, output):
    
        rgb_im = self.get_input_port(4).Eval(context).data
        plt.imshow(rgb_im)
        plt.show()

        #image_pil = Image.open(rgb_im).convert("RGB")
        image_pil = PILImage.fromarray(rgb_im).convert("RGB")
        plt.imshow(image_pil)
        plt.show()
        text_prompt = 'canned tomato'
        masks, boxes, phrases, logits = self.lang_sam_model.predict(image_pil, text_prompt)
        print("masks", masks)
        print("masks shape", masks.shape)
        print("boxes", boxes)
        print("phrases", phrases)
        print("logits", logits)

        smallest_sum = [2**30,0,0] # value and coordinates
        largest_sum = [0,0,0]

        for x in range(masks[0].shape[0]):
            for y in range(masks[0].shape[1]):
                if masks[0][x][y] == True and x + y > largest_sum[0]:
                    largest_sum = [x+y, x, y]
                if masks[0][x][y] == True and x + y < smallest_sum[0]:
                    smallest_sum = [x+y, x, y]

        print("smallest sum", smallest_sum)
        print("largest sum", largest_sum)

        np_image = np.array(image_pil)
        result = draw_image(np_image, masks, boxes, phrases)
        plt.imshow(result)
        plt.show()

        

                    


        #rgb_im = self.station.GetOutputPort('camera{}_rgb_image'.format(1)).Eval(self.cntxt31).data
        #rgb_im = self.station.GetOutputPort('camera{}_rgb_image'.format(1))
        #print(type(rgb_im))

        depth_im = self.get_input_port(5).Eval(context).data.squeeze()
        #depth_im[depth_im == np.inf] = 50.0
        print("DEPTH IMAGE", depth_im)
        print("DEPTH SHAPE", depth_im.shape)

        img_h, img_w = depth_im.shape
        v_range = np.arange(img_h)
        u_range = np.arange(img_w)
        depth_u, depth_v = np.meshgrid(u_range, v_range)
        depth_pnts = np.dstack([depth_v, depth_u, depth_im])
        depth_pnts = depth_pnts.reshape([img_h * img_w, 3])
        # point poses in camera frame
        pC = self.project_depth_to_pC(depth_pnts)
        print("PC", pC)
        print("PC SHAPE", pC.shape)
        pC = np.reshape(pC,(480,640,3))
        print("PC", pC)
        print("PC SHAPE", pC.shape)




        # res = self.model([Tf.to_tensor(rgb_im[:, :, :3]).to(self.device)])

        # print(res[0].keys())
        # print(res[0]["masks"])
        # print(res[0]["labels"])
        # print(res[0]["scores"])

        # plt.imshow(rgb_im)
        # plt.show()


        # for k in res[0].keys():
        #     if k == "masks":
        #         res[0][k] = res[0][k].mul(
        #             255).byte().cpu().numpy()
        #     else:
        #         res[0][k] = res[0][k].cpu().numpy()

        # mustard_ycb_idx = 3
        # mask_idx = np.argmax(res[0]['labels'] == mustard_ycb_idx)
        # mask = res[0]['masks'][mask_idx,0]

        # plt.imshow(mask)
        # plt.title("Mask from Camera " + str(i))
        # plt.colorbar()
        # plt.show()

        print("CROP POINTS", pC[smallest_sum[1]][smallest_sum[2]], pC[largest_sum[1]][largest_sum[2]])

        X_Crop1 = self.X_WC_Cam1 @ RigidTransform(pC[smallest_sum[1]][smallest_sum[2]])
        print("CROPPED FRAME1", X_Crop1)
        X_Crop2 = self.X_WC_Cam1 @ RigidTransform(pC[largest_sum[1]][largest_sum[2]])
        print("CROPPED FRAME2", X_Crop2)

        # Solves: Failure at perception/point_cloud.cc:350 in Crop(): condition '(lower_xyz.array() <= upper_xyz.array()).all()' failed.
        X_Crop1_Array = [X_Crop1.translation()[0], X_Crop1.translation()[1], X_Crop1.translation()[2]]
        X_Crop2_Array = [X_Crop2.translation()[0], X_Crop2.translation()[1], X_Crop2.translation()[2]]

        print(type(X_Crop1_Array))
        print(X_Crop1_Array)
        print(X_Crop2_Array)

        if X_Crop1_Array[0] > X_Crop2_Array[0]:
            tmp = X_Crop2_Array[0]
            X_Crop2_Array[0] = X_Crop1_Array[0]
            X_Crop1_Array[0] = tmp
        if X_Crop1_Array[1] > X_Crop2_Array[1]:
            tmp = X_Crop2_Array[1]
            X_Crop2_Array[1] = X_Crop1_Array[1]
            X_Crop1_Array[1] = tmp
        if X_Crop1_Array[2] > X_Crop2_Array[2]:
            tmp = X_Crop2_Array[2]
            X_Crop2_Array[2] = X_Crop1_Array[2]
            X_Crop1_Array[2] = tmp

        print(X_Crop1_Array)
        print(X_Crop2_Array)
        X_Crop1_Array[0] = X_Crop1_Array[0] - 0.025
        X_Crop1_Array[1] = X_Crop1_Array[1] - 0.025
        X_Crop1_Array[2] = X_Crop1_Array[2] - 0.025

        X_Crop2_Array[0] = X_Crop2_Array[0] + 0.025
        X_Crop2_Array[1] = X_Crop2_Array[1] + 0.025
        X_Crop2_Array[2] = X_Crop2_Array[2] + 0.025

        print(X_Crop1_Array)
        print(X_Crop2_Array)

        body_poses = self.get_input_port(3).Eval(context)
        pcd = []
        for i in range(3):
            cloud = self.get_input_port(i).Eval(context)
            #pcd.append(cloud.Crop(self._crop_lower, self._crop_upper))
            pcd.append(cloud.Crop(np.array(X_Crop1_Array), np.array(X_Crop2_Array)))
            pcd[i].EstimateNormals(radius=0.1, num_closest=30)

            # Flip normals toward camera
            X_WC = body_poses[self._camera_body_indices[i]]
            pcd[i].FlipNormalsTowardPoint(X_WC.translation())
        merged_pcd = Concatenate(pcd)
        down_sampled_pcd = merged_pcd.VoxelizedDownSample(voxel_size=0.005)

        costs = []
        X_Gs = []
        # TODO(russt): Take the randomness from an input port, and re-enable
        # caching.
        for i in range(100 if self.running_as_notebook else 2):
            cost, X_G = GenerateAntipodalGraspCandidate(
                self._internal_model, self._internal_model_context,
                down_sampled_pcd, self._rng)
            if np.isfinite(cost):
                costs.append(cost)
                X_Gs.append(X_G)

        if len(costs) == 0:
            # Didn't find a viable grasp candidate
            print("Didn't find a viable grasp candidate")
            X_WG = RigidTransform(RollPitchYaw(-np.pi / 2, 0, np.pi / 2),
                                  [0.5, 0, 0.22])
            output.set_value((np.inf, X_WG))
        else:
            best = np.argmin(costs)
            output.set_value((costs[best], X_Gs[best]))


