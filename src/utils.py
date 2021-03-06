from __future__ import division
import numpy as np
import nibabel as nib
import copy
from scipy.ndimage import rotate
from skimage.transform import resize
from scipy.ndimage import measurements
import tensorflow as tf


##############################
# process .nii data
def load_data_pairs(pair_list, resize_r, rename_map):
    """load all volume pairs"""
    img_clec = []
    label_clec = []

    # rename_map = [0, 205, 420, 500, 550, 600, 820, 850]
    for k in range(0, len(pair_list), 2):
        img_path = pair_list[k]
        lab_path = pair_list[k+1]#很粗糙了，直接下一个作为label
        img_data = nib.load(img_path).get_data().copy()#输入ingdata
        lab_data = nib.load(lab_path).get_data().copy()#输入labeldata

        ###preprocessing
        # resize
        resize_dim = (np.array(img_data.shape) * resize_r).astype('int')
        #print("resize后的dim：",resize_dim)[307 307 143]这里是因为r等于0.6,512×0.6=307
        img_data = resize(img_data, resize_dim, order=1, preserve_range=True)
        lab_data = resize(lab_data, resize_dim, order=0, preserve_range=True)
        #print("resize后大小：",img_data.shape)[307 307 143]
        lab_r_data = np.zeros(lab_data.shape, dtype='int32')

        # rename labels
        for i in range(len(rename_map)):
            lab_r_data[lab_data == rename_map[i]] = i
            #将区间设置为0-7,用于产生one-hot编码
        # for s in range(img_data.shape[2]):
        #     cv2.imshow('img', np.concatenate(((img_data[:,:,s]).astype('uint8'), (lab_r_data[:,:,s]*30).astype('uint8')), axis=1))
        #     cv2.waitKey(20)

        img_clec.append(img_data)
        label_clec.append(lab_r_data)

    return img_clec, label_clec


def get_batch_patches(img_clec, label_clec, patch_dim, batch_size, chn=1, flip_flag=True, rot_flag=True):
    """generate a batch of paired patches for training"""
    batch_img = np.zeros([batch_size, patch_dim, patch_dim, patch_dim, chn]).astype('float32')
    batch_label = np.zeros([batch_size, patch_dim, patch_dim, patch_dim]).astype('int32')
    #patch_dim和input_size大小相同
    for k in range(batch_size):
        # randomly select an image pair
        rand_idx = np.arange(len(img_clec))#[1 2 3 4 5 6 7]
        np.random.shuffle(rand_idx)#顺序重新排一下
        rand_img = img_clec[rand_idx[0]]
        rand_label = label_clec[rand_idx[0]]#取出一个体数据
        rand_img = rand_img.astype('float32')
        rand_label = rand_label.astype('int32')

        # randomly select a box anchor
        l, w, h = rand_img.shape
        l_rand = np.arange(l - patch_dim)#随机选择一个值作为起点,这里是生成了一个起点序列
        w_rand = np.arange(w - patch_dim)
        h_rand = np.arange(h - patch_dim)
        np.random.shuffle(l_rand)#对起点序列进行随机打乱
        np.random.shuffle(w_rand)
        np.random.shuffle(h_rand)
        pos = np.array([l_rand[0], w_rand[0], h_rand[0]])#获得起点坐标
        # crop 从而产生一个对应的图像数据
        img_temp = copy.deepcopy(rand_img[pos[0]:pos[0]+patch_dim, pos[1]:pos[1]+patch_dim, pos[2]:pos[2]+patch_dim])

        # normalization 进行标准化，感觉很粗糙啊
        img_temp = img_temp/255.0
        mean_temp = np.mean(img_temp)
        dev_temp = np.std(img_temp)
        img_norm = (img_temp - mean_temp) / dev_temp
        #标签也裁剪出和原始图像对应的一块大小
        label_temp = copy.deepcopy(rand_label[pos[0]:pos[0]+patch_dim, pos[1]:pos[1]+patch_dim, pos[2]:pos[2]+patch_dim])


        # rotation 随机进行旋转
        if rot_flag and np.random.random() > 0.65:
            # print 'rotating patch...'
            rand_angle = [-25, 25]
            np.random.shuffle(rand_angle)
            img_norm = rotate(img_norm, angle=rand_angle[0], axes=(1, 0), reshape=False, order=1)
            label_temp = rotate(label_temp, angle=rand_angle[0], axes=(1, 0), reshape=False, order=0)
        #贴到生成的样本
        batch_img[k, :, :, :, chn-1] = img_norm
        batch_label[k, :, :, :] = label_temp

    return batch_img, batch_label


# calculate the cube information
def fit_cube_param(vol_dim, cube_size, ita):
    dim = np.asarray(vol_dim)
    #print( "dim:", str( dim) ) dim: [307 307 143]
    # cube number and overlap along 3 dimensions
    fold = dim / cube_size + ita
    #print( "fold:", str( fold ) )#[ 7.19791667  7.19791667  5.48958333]--这一个是307/96+4得来的结果
    ovlap = np.ceil(np.true_divide((fold * cube_size - dim), (fold - 1)))#dim+ita*cubesize-dim 在
    #print( "ovlap:", str( ovlap ) )#[62. 62. 86.]
    ovlap = ovlap.astype('int')
    #print( "ovlap:", str( ovlap ) )#[62 62 86]
    fold = np.ceil(np.true_divide((dim + (fold - 1)*ovlap), cube_size))
    fold = fold.astype('int')
    #print( "fold:", str( fold) ) fold: [8 8 6]
    return fold, ovlap


# decompose volume into list of cubes
def decompose_vol2cube(vol_data, batch_size, cube_size, n_chn, ita):#最后一个参数是立方体重叠因子
    cube_list = []
    # get parameters for decompose
    #print("这三个玩意大小分别为:",str(vol_data.shape), str(cube_size), str(ita))
    #(307, 307, 143) 96 4 cube_size是输入神经网络的大小
    fold, ovlap = fit_cube_param(vol_data.shape, cube_size, ita)
    dim = np.asarray(vol_data.shape)#[307, 307, 143]
    # decompose
    for R in range(0, fold[0]):#在第一个维度切分
        r_s = R*cube_size - R*ovlap[0]#s是begin的位置
        r_e = r_s + cube_size
        if r_e >= dim[0]:#超出啦边界
            r_s = dim[0] - cube_size
            r_e = r_s + cube_size
        for C in range(0, fold[1]):
            c_s = C*cube_size - C*ovlap[1]
            c_e = c_s + cube_size
            if c_e >= dim[1]:
                c_s = dim[1] - cube_size
                c_e = c_s + cube_size
            for H in range(0, fold[2]):
                h_s = H*cube_size - H*ovlap[2]
                h_e = h_s + cube_size
                if h_e >= dim[2]:
                    h_s = dim[2] - cube_size
                    h_e = h_s + cube_size
                # partition multiple channels
                cube_temp = vol_data[r_s:r_e, c_s:c_e, h_s:h_e]
                cube_batch = np.zeros([batch_size, cube_size, cube_size, cube_size, n_chn]).astype('float32')
                cube_batch[0, :, :, :, 0] = copy.deepcopy(cube_temp)
                # save
                cube_list.append(cube_batch)

    return cube_list


# compose list of label cubes into a label volume
def compose_label_cube2vol(cube_list, vol_dim, cube_size, ita, class_n):
    # get parameters for compose
    fold, ovlap = fit_cube_param(vol_dim, cube_size, ita)
    # create label volume for all classes
    label_classes_mat = (np.zeros([vol_dim[0], vol_dim[1], vol_dim[2], class_n])).astype('int32')
    idx_classes_mat = (np.zeros([cube_size, cube_size, cube_size, class_n])).astype('int32')

    p_count = 0
    for R in range(0, fold[0]):
        r_s = R*cube_size - R*ovlap[0]
        r_e = r_s + cube_size
        if r_e >= vol_dim[0]:
            r_s = vol_dim[0] - cube_size
            r_e = r_s + cube_size
        for C in range(0, fold[1]):
            c_s = C*cube_size - C*ovlap[1]
            c_e = c_s + cube_size
            if c_e >= vol_dim[1]:
                c_s = vol_dim[1] - cube_size
                c_e = c_s + cube_size
            for H in range(0, fold[2]):
                h_s = H*cube_size - H*ovlap[2]
                h_e = h_s + cube_size
                if h_e >= vol_dim[2]:
                    h_s = vol_dim[2] - cube_size
                    h_e = h_s + cube_size
                # histogram for voting
                for k in range(class_n):
                    idx_classes_mat[:, :, :, k] = (cube_list[p_count] == k)
                # accumulation
                label_classes_mat[r_s:r_e, c_s:c_e, h_s:h_e, :] = label_classes_mat[r_s:r_e, c_s:c_e, h_s:h_e, :] + idx_classes_mat

                p_count += 1
    # print 'label mat unique:'
    # print np.unique(label_mat)

    compose_vol = np.argmax(label_classes_mat, axis=3)
    # print np.unique(label_mat)

    return compose_vol


# compose list of probability cubes into a probability volumes
def compose_prob_cube2vol(cube_list, vol_dim, cube_size, ita, class_n):
    # get parameters for compose
    fold, ovlap = fit_cube_param(vol_dim, cube_size, ita)
    # create label volume for all classes
    map_classes_mat = (np.zeros([vol_dim[0], vol_dim[1], vol_dim[2], class_n])).astype('float32')
    cnt_classes_mat = (np.zeros([vol_dim[0], vol_dim[1], vol_dim[2], class_n])).astype('float32')

    p_count = 0
    for R in range(0, fold[0]):
        r_s = R*cube_size - R*ovlap[0]
        r_e = r_s + cube_size
        if r_e >= vol_dim[0]:
            r_s = vol_dim[0] - cube_size
            r_e = r_s + cube_size
        for C in range(0, fold[1]):
            c_s = C*cube_size - C*ovlap[1]
            c_e = c_s + cube_size
            if c_e >= vol_dim[1]:
                c_s = vol_dim[1] - cube_size
                c_e = c_s + cube_size
            for H in range(0, fold[2]):
                h_s = H*cube_size - H*ovlap[2]
                h_e = h_s + cube_size
                if h_e >= vol_dim[2]:
                    h_s = vol_dim[2] - cube_size
                    h_e = h_s + cube_size
                # accumulation
                map_classes_mat[r_s:r_e, c_s:c_e, h_s:h_e, :] = map_classes_mat[r_s:r_e, c_s:c_e, h_s:h_e, :] + cube_list[p_count]
                cnt_classes_mat[r_s:r_e, c_s:c_e, h_s:h_e, :] = cnt_classes_mat[r_s:r_e, c_s:c_e, h_s:h_e, :] + 1.0

                p_count += 1

    # elinimate NaN
    nan_idx = (cnt_classes_mat == 0)
    cnt_classes_mat[nan_idx] = 1.0
    # average
    compose_vol = map_classes_mat / cnt_classes_mat

    return compose_vol

# Remove small connected components
def remove_minor_cc(vol_data, rej_ratio, rename_map):
    """Remove small connected components refer to rejection ratio"""
    """Usage
        # rename_map = [0, 205, 420, 500, 550, 600, 820, 850]
        # nii_path = '/home/xinyang/project_xy/mmwhs2017/dataset/ct_output/test/test_4.nii'
        # vol_file = nib.load(nii_path)
        # vol_data = vol_file.get_data().copy()
        # ref_affine = vol_file.affine
        # rem_vol = remove_minor_cc(vol_data, rej_ratio=0.2, class_n=8, rename_map=rename_map)
        # # save
        # rem_path = 'rem_cc.nii'
        # rem_vol_file = nib.Nifti1Image(rem_vol, ref_affine)
        # nib.save(rem_vol_file, rem_path)

        #===# possible be parallel in future
    """

    rem_vol = copy.deepcopy(vol_data)
    class_n = len(rename_map)
    # retrieve all classes
    for c in range(1, class_n):
        print ('processing class %d...' % c)

        class_idx = (vol_data==rename_map[c])*1
        class_vol = np.sum(class_idx)
        labeled_cc, num_cc = measurements.label(class_idx)
        # retrieve all connected components in this class
        for cc in range(1, num_cc+1):
            single_cc = ((labeled_cc==cc)*1)
            single_vol = np.sum(single_cc)
            # remove if too small
            if single_vol / (class_vol*1.0) < rej_ratio:
                rem_vol[labeled_cc==cc] = 0

    return rem_vol

#Stage*BLOCKS*Columns 主要用作生成全局路径
def produce_global_path_list(StageNum,Blocks,Columns):
    STAGE_LIST=[]
    for i in range (0,StageNum):
        BLOCK_LIST=[]
        for J in range(0, Blocks):
            ONE_BLOCK = np.zeros(Columns)  # 对应柱的数量
            ONE_BLOCK[np.random.randint(0, Columns)] = 1.0  # 为global path选中唯一的一条路径
            BLOCK_LIST.append(ONE_BLOCK)
        STAGE_LIST.append(BLOCK_LIST)
    return STAGE_LIST

#StageNum*BLOCKS*Columns 用于产生全局路径
def produce_global_path_list(StageNum,Blocks,Columns):
    STAGE_LIST=[]
    for i in range (0,StageNum):
        BLOCK_LIST=[]
        for j in range(0, Blocks):
            ONE_BLOCK = np.zeros(Columns)  # 对应柱的数量
            ONE_BLOCK[np.random.randint(0, Columns)] = 1  # 为global path选中唯一的一条路径
            BLOCK_LIST.append(ONE_BLOCK)
        STAGE_LIST.append(BLOCK_LIST)
    return STAGE_LIST
#

#用于训练时随机选择是否进入全局路径，StageNum*Blocks threshold越大，那么选中全局路径的可能性越大
def train_is_global_path_list(StageNum,Blocks,threshold=5):
    STAGE_LIST = []
    for i in range(0, StageNum):
        BLOCK_LIST = []
        for j in range(0, Blocks):
            if np.random.randint(0, 10)>=threshold:
                BLOCK_LIST.append(0.0)
            else:
                BLOCK_LIST.append(1.0)
        STAGE_LIST.append(BLOCK_LIST)
    return STAGE_LIST
#测试时不会进入全局路径，直接append 0.0即可
def test_is_global_path_list(StageNum,Blocks):
    STAGE_LIST = []
    for i in range(0, StageNum):
        BLOCK_LIST = []
        for j in range(0, Blocks):
           BLOCK_LIST.append(0.0)
        STAGE_LIST.append(BLOCK_LIST)
    return STAGE_LIST

#以0.5的概率选中任何一条路径
def train_local_path_list(StageNum,Blocks,Columns,threshold=5):
    STAGE_LIST = []
    for i in range(0, StageNum):
        BLOCK_LIST = []
        for j in range(0, Blocks):
            ROW_LIST=[]
            for k in range(0,2**(Columns-1)):
                ONE_PATH=[]
                for l in range(Columns):
                    if np.random.randint(0, 10)>=threshold:
                        ONE_PATH.append(0.0)
                    else:
                        ONE_PATH.append(1.0)
                ROW_LIST.append(ONE_PATH)#每一行对应一条局部路径
            BLOCK_LIST.append(ROW_LIST)#一个block对应多行
        STAGE_LIST.append(BLOCK_LIST)#一个stage可以对应多个block
    return STAGE_LIST

#所有路径都被选中
def test_local_path_list(StageNum,Blocks,Columns,threshold=5):
    STAGE_LIST = []
    for i in range(0, StageNum):
        BLOCK_LIST = []
        for j in range(0, Blocks):
            ROW_LIST=[]
            for k in range(0,2**(Columns-1)):
                ONE_PATH=[]
                for l in range(Columns):
                        ONE_PATH.append(1.0)
                ROW_LIST.append(ONE_PATH)#每一行对应一条局部路径
            BLOCK_LIST.append(ROW_LIST)#一个block对应多行
        STAGE_LIST.append(BLOCK_LIST)#一个stage可以对应多个block
    return STAGE_LIST
#获取用于产生训练数据的路径(路径被随机丢弃)
def get_train_path_list(StageNum,Blocks,Columns):
    is_global_path=train_is_global_path_list(StageNum, Blocks)
    global_path_list=produce_global_path_list(StageNum,Blocks,Columns)
    local_path_list=train_local_path_list(StageNum,Blocks,Columns)
    return is_global_path, global_path_list, local_path_list

#获取用于产生测试数据的路径（路径全部保留）
def get_test_path_list(StageNum,Blocks,Columns):
    is_global_path=test_is_global_path_list(StageNum, Blocks)
    global_path_list=produce_global_path_list(StageNum,Blocks,Columns)
    local_path_list=test_local_path_list(StageNum,Blocks,Columns)
    return is_global_path, global_path_list, local_path_list
#获取需要保留的背景样本数量
def background_num_to_save(input_gt,pred):#这里groundtruth是已经one-hot编码的结果
    background_num = tf.reduce_sum(input_gt[:, :, :, :,0])#这是因为只有对应分到这一类才为1
    total_num=tf.reduce_sum(input_gt)
    foreground_num=total_num-background_num
    save_back_ground_num=tf.reduce_max([2*foreground_num, background_num/8])#设定需要保留的背景样本数量
    save_back_ground_num=tf.clip_by_value(save_back_ground_num, 0, background_num)#保证待保留的数量不要超标,最多和原来背景一样多
    return save_back_ground_num

def no_background(input_gt):
    return input_gt

def exist_background(input_gt, pred,save_back_ground_num):
    #硬负样本：在标签中属于负样本，但是在预测中被极大预测为前景（也就是被预测为背景概率值很小），
    #这里需要注意一点，先行筛选掉那些属于前景类的样本，因为他们本来被预测为背景的概率就很低
    #如果不需要筛选，那么直接对背景概率值求反然后找出最大的几个那就是背景概率值最小的了，
    #但是这里需要筛选，那么我们就想办法，让属于前景类样本被预测为背景值永远大于背景类样本，如此，便可以直接对所有样本找背景概率值最小的那几个即可完成硬负采样任务
    batch, in_depth, in_height, in_width, in_channels = [int(d) for d in input_gt.get_shape()]#取出各维度大小
    pred_data = pred[:, :, :, :, 0]  # 将输出结果属于背景的拎出来，因为需要它生成mask
    gt_backgound_data=1-input_gt[:, :, :, :, 0]#这样标签中原本属于背景类的全部为0，原本属于前景类的全部为1
    pred_back_ground_data = tf.reshape(pred_data, (batch, in_depth * in_height * in_width))  # 把数据按照batch为维度进行展开
    gt_back_ground_data=tf.reshape(gt_backgound_data, (batch, in_depth * in_height * in_width))#对GT进行reshape
    new_pred_data=pred_back_ground_data+gt_back_ground_data#这样预测结果中，标签属于前景类的预测值被加一，标签属于背景类的元素值不变，
    #这样新产生的预测结果中属于前景类标签所产生的概率值一定大于背景类
    mask = []
    for i in range(batch):
        gti = -1*new_pred_data[i, :]  # 取出一个批次的数据 ，由于取了反，
        #这样子背景类的预测概率值一定大于前景类，并且原本预测概率值较小的背景类此时由于取了反有更大值
        max_k_number, index = tf.nn.top_k(gti, save_back_ground_num)  # 找出最大的前k个值，这里也就是找出了硬负样本
        max_k = tf.reduce_min(max_k_number)  # 找出第k大值
        one = tf.ones_like(gti)  # 全1掩码
        zero = tf.zeros_like(gti)  # 全0掩码
        mask_slice = tf.where(gti < max_k, x=zero, y=one)  # 小于k的位置为0，大于k的位置为1
        mask_slice = tf.reshape(mask_slice, [in_depth, in_height, in_width])
        mask.append(mask_slice)
    mask = tf.expand_dims(mask, -1)  # -1表示最后一维，这是生成针对背景的掩码
    other_mask = tf.ones([batch, in_depth, in_height, in_width, in_channels - 1], tf.float32)  # 其他维补充上来
    full_mask = tf.concat([mask, other_mask], 4)  # 形成丢弃背景信息的掩码

    input_gt = full_mask * input_gt  # 形成丢弃背景信息的groundtruth
    return input_gt

#为groundtruth生成背景掩码，从而丢弃掉不需要的背景信息
def produce_mask_background(input_gt,pred):
    save_back_ground_num=background_num_to_save(input_gt,pred)#根据groundtruth获取获取需要保留的背景数量
    save_back_ground_num = tf.cast(save_back_ground_num, dtype=tf.int32)#转换成int
    product = tf.cond(save_back_ground_num < 5, lambda:no_background(input_gt),lambda: exist_background(input_gt, pred,save_back_ground_num))
    # 条件满足，就会产生前面那个，进入限制产生背景样本，否则不对背景样本产生mask
    return product