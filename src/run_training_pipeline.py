import gc
import os
import sys
from datetime import datetime
from tqdm import tqdm
import mlflow
import numpy as np
import pytorch_lightning as pl
import torch
import yaml
from pytorch_lightning.callbacks import (
    EarlyStopping,
    LearningRateMonitor,
    ModelCheckpoint,
)
from torch.utils.data import DataLoader
from yaml.loader import SafeLoader

from classes.data.satellite_image import SatelliteImage
from classes.labelers.labeler import BDTOPOLabeler, RILLabeler
from classes.optim.losses import CrossEntropy
from classes.optim.optimizer import generate_optimization_elements
from data.components.change_detection_dataset import ChangeIsEverywhereDataset
from data.components.dataset import PleiadeDataset
from models.components.segmentation_models import DeepLabv3Module
from models.segmentation_module import SegmentationModule
from train_pipeline_utils.download_data import load_satellite_data
from train_pipeline_utils.handle_dataset import (
    generate_transform, 
    split_dataset
)
from train_pipeline_utils.prepare_data import (  # , write_splitted_images_mask
    check_labelled_images,
    filter_images,
    label_images,
    save_images_and_masks,
    split_images,
)
from utils.utils import update_storage_access
from rasterio.errors import RasterioIOError

def download_data(config):
    """
    Downloads data based on the given configuration.

    Args:
        config: a dictionary representing the
        configuration information for data download.

    Returns:
        A list of output directories for each downloaded dataset.
    """

    print("Entre dans la fonction download_data")
    config_data = config["donnees"]
    list_output_dir = []

    years = config_data["year"]
    deps = config_data["dep"]
    src = config_data["source train"]

    for year, dep in zip(years, deps):
        output_dir = load_satellite_data(year, dep, src)
        list_output_dir.append(output_dir)

    return list_output_dir


def prepare_data(config, list_data_dir):
    """
    Preprocesses and splits the raw input images
    into tiles and corresponding masks,
    and saves them in the specified output directories.

    Args:
        config: A dictionary representing the configuration settings.
        list_data_dir: A list of strings representing the paths
        to the directories containing the raw input image files.

    Returns:
        A list of strings representing the paths to
        the output directories containing the
        preprocessed tile and mask image files.
    """

    print("Entre dans la fonction prepare_data")
    config_data = config["donnees"]

    years = config_data["year"]
    deps = config_data["dep"]
    src = config_data["source train"]
    labeler = config_data["type labeler"]

    list_output_dir = []

    for i, (year, dep) in enumerate(zip(years, deps)):
        # i, year , dep= 0,years[0],deps[0]
        date = datetime.strptime(str(year) + "0101", "%Y%m%d")
        if labeler == "RIL":
            buffer_size = config_data["buffer size"]
            labeler = RILLabeler(date, dep=dep, buffer_size=buffer_size)
        elif labeler == "BDTOPO":
            labeler = BDTOPOLabeler(date, dep=dep)

        output_dir = (
            "train_data" + "-" + src + "-" + dep + "-" + str(year) + "/"
        )

        if not check_labelled_images(output_dir):
            
            # list_splitted_images = split_images(
            #     list_data_dir[i], config_data["n channels train"]
            # )
            dir = list_data_dir[i]
            list_path = [dir + "/" + filename for filename in os.listdir(dir)]
            
            for path in tqdm(list_path):
                # path = list_path[0]
                try:
                    si = SatelliteImage.from_raster(
                        file_path=path,
                        dep=dep,
                        date=date,
                        n_bands=config_data["n channels train"]
                    )
                
                except RasterioIOError:
                    print("Errerur de lecture du fichier " + path)
                    continue

                else:
                    list_splitted_images = si.split(config_data["tile size"]) 
                    
                    list_filtered_splitted_images = filter_images(
                        config_data["source train"], list_splitted_images
                    )

                    list_filtered_splitted_labeled_images, list_masks = label_images(
                        list_filtered_splitted_images, labeler
                    )

                    save_images_and_masks(
                        list_filtered_splitted_labeled_images, list_masks, output_dir
                    )

        list_output_dir.append(output_dir)

    return list_output_dir


def intantiate_dataset(config, list_path_images, list_path_labels):
    """
    Instantiates the appropriate dataset object
    based on the configuration settings.

    Args:
        config: A dictionary representing the configuration settings.
        list_path_images: A list of strings representing
        the paths to the preprocessed tile image files.
        list_path_labels: A list of strings representing
        the paths to the corresponding preprocessed mask image files.

    Returns:
        A dataset object of the specified type.
    """

    dataset_dict = {
        "pleiadeDataset": PleiadeDataset,
        "changeIsEverywhere": ChangeIsEverywhereDataset,
    }

    dataset_type = config["donnees"]["dataset"]

    # inqtanciation du dataset comple
    if dataset_type not in dataset_dict:
        raise ValueError("Invalid dataset type")
    else:
        full_dataset = dataset_dict[dataset_type](
            list_path_images, list_path_labels
        )

    return full_dataset


def intantiate_dataloader(config, list_output_dir):
    """
    Instantiates and returns the data loaders for
    training, validation, and testing datasets.

    Args:
    - config (dict): A dictionary containing the configuration parameters
    for data loading and processing.
    - list_output_dir (list): A list of strings containing the paths to
    the directories that contain the training data.

    Returns:
    - train_dataloader (torch.utils.data.DataLoader):
    The data loader for the training dataset.
    - valid_dataloader (torch.utils.data.DataLoader):
    The data loader for the validation dataset.
    - test_dataloader (torch.utils.data.DataLoader):
    The data loader for the testing dataset.

    The function first generates the paths for the image and label data
    based on the data source (Sentinel, PLEIADES) vs pre-annotated datasets.
    It then instantiates the required dataset class
    (using the `intantiate_dataset` function) and splits the full dataset
    into training and validation datasets based on the validation proportion
    specified in the configuration parameters.

    Next, the appropriate transformations are applied to the training
    and validation datasets using the `generate_transform` function.

    Finally, the data loaders for the training and validation datasets
    are created using the `DataLoader` class from the PyTorch library,
    and the data loader for the testing dataset is set to `None`.
    """
    # génération des paths en fonction du type de Données
    # (Sentinel, PLEIADES) VS Dataset préannotés

    if config["donnees"]["source train"] in ["PLEIADES", "SENTINEL2"]:
        list_path_labels = []
        list_path_images = []
        for dir in list_output_dir:
            labels = os.listdir(dir + "/labels")
            images = os.listdir(dir + "/images")

            list_path_labels = np.concatenate(
                (
                    list_path_labels,
                    np.sort([dir + "/labels/" + name for name in labels]),
                )
            )

            list_path_images = np.concatenate(
                (
                    list_path_images,
                    np.sort([dir + "/images/" + name for name in images]),
                )
            )

    # récupération de la classe de Dataset souhaitée
    full_dataset = intantiate_dataset(
        config, list_path_images, list_path_labels
    )
    train_dataset, valid_dataset = split_dataset(
        full_dataset, config["optim"]["val prop"]
    )

    # on applique les transforms respectives
    augmentation = config["donnees"]["augmentation"]
    tile_size = config["donnees"]["tile size"]
    t_aug, t_preproc = generate_transform(tile_size, augmentation)
    train_dataset.transforms = t_aug
    valid_dataset.transforms = t_preproc

    # création des dataloader
    batch_size = config["optim"]["batch size"]

    train_dataloader, valid_dataloader = [
        DataLoader(
            ds,
            batch_size=batch_size,
            shuffle=boolean,
            num_workers=2,
        )
        for ds, boolean in zip([train_dataset, valid_dataset], [True, False])
    ]

    # instancier le dataset de test
    # TO DO dépendra du nombre dimages dans le data set de test
    # et de la tile_size
    # test_batch_size = None
    # dataset_test = None
    test_dataloader = None
    return train_dataloader, valid_dataloader, test_dataloader


def instantiate_model(config):
    """
    Instantiate a module based on the provided module type.

    Args:
        module_type (str): Type of module to instantiate.

    Returns:
        object: Instance of the specified module.
    """
    module_type = config["optim"]["module"]
    module_dict = {"deeplabv3": DeepLabv3Module}
    nchannel = config["donnees"]["n channels train"]

    if module_type not in module_dict:
        raise ValueError("Invalid module type")

    return module_dict[module_type](nchannel)


def instantiate_loss(config):
    """
    intantiates an optimizer object with the parameters
    specified in the configuration file.

    Args:
        model: A PyTorch model object.
        config: A dictionary object containing the configuration parameters.

    Returns:
        An optimizer object from the `torch.optim` module.
    """
    loss_type = config["optim"]["loss"]
    loss_dict = {"crossentropy": CrossEntropy}

    if loss_type not in loss_dict:
        raise ValueError("Invalid loss type")
    else:
        return loss_dict[loss_type]()


def instantiate_lightning_module(config, model):
    """
    Create a PyTorch Lightning module for segmentation
    with the given model and optimization configuration.

    Args:
        config (dict): Dictionary containing the configuration
        parameters for optimization.
        model: The PyTorch model to use for segmentation.

    Returns:
        A PyTorch Lightning module for segmentation.
    """
    list_params = generate_optimization_elements(config)

    lightning_module = SegmentationModule(
        model=model,
        loss=instantiate_loss(config),
        optimizer=list_params[0],
        optimizer_params=list_params[1],
        scheduler=list_params[2],
        scheduler_params=list_params[3],
        scheduler_interval=list_params[4],
    )
    return lightning_module


def instantiate_trainer(config, lightning_module):
    """
    Create a PyTorch Lightning module for segmentation with
    the given model and optimization configuration.

    Args:
        config (dict): Dictionary containing the configuration
        parameters for optimization.
        model: The PyTorch model to use for segmentation.

    Returns:
        SegmentationModule: A PyTorch Lightning module for segmentation.
    """
    # def callbacks
    checkpoint_callback = ModelCheckpoint(
        monitor="validation_loss", save_top_k=1, save_last=True, mode="max"
    )
    early_stop_callback = EarlyStopping(
        monitor="validation_loss", mode="max", patience=3
    )
    lr_monitor = LearningRateMonitor(logging_interval="step")
    list_callbacks = [lr_monitor, checkpoint_callback, early_stop_callback]

    strategy = "auto"

    trainer = pl.Trainer(
        callbacks=list_callbacks,
        max_epochs=config["optim"]["max epochs"],
        num_sanity_val_steps=2,
        strategy=strategy,
        log_every_n_steps=2,
    )

    return trainer


def run_pipeline(remote_server_uri, experiment_name, run_name):
    """
    Runs the segmentation pipeline u
    sing the configuration specified in `config.yml`
    and the provided MLFlow parameters.
    Args:
        None
    Returns:
        None
    """
    # Open the file and load the file
    with open("../config.yml") as f:
        config = yaml.load(f, Loader=SafeLoader)

    list_data_dir = download_data(config)
    # list_data_dir = ['/home/onyxia/work/detection-habitat-spontane/data/PLEIADES/2022/MARTINIQUE']
    # list_data_dir = ['/home/onyxia/work/detection-habitat-spontane/data/SENTINEL2/MAYOTTE/TUILES_2022']
    # list_data_dir = [
    #     "/home/onyxia/work/detection-habitat-spontane/data/PLEIADES/2022/GUYANE"
    # ]
    list_output_dir = prepare_data(config, list_data_dir)
  
    model = instantiate_model(config)

    train_dl, valid_dl, test_dl = intantiate_dataloader(
        config, list_output_dir
    )

    light_module = instantiate_lightning_module(config, model)
    trainer = instantiate_trainer(config, light_module)

    # trainer.test(lightning_module,test_dataloader) TO DO
    torch.cuda.empty_cache()
    gc.collect()

    if config["mlflow"]:
        update_storage_access()
        os.environ["MLFLOW_S3_ENDPOINT_URL"] = "https://minio.lab.sspcloud.fr"
        mlflow.end_run()
        mlflow.set_tracking_uri(remote_server_uri)
        mlflow.set_experiment(experiment_name)
        # mlflow.pytorch.autolog()

        with mlflow.start_run(run_name=run_name):
            mlflow.log_artifact("../config.yml", artifact_path="config.yml")
            trainer.fit(light_module, train_dl, valid_dl)
            # trainer.test(light_module, test_dl)
    else:
        trainer.fit(light_module, train_dl, valid_dl)
        # trainer.test(light_module, test_dl)


if __name__ == "__main__":
    # MLFlow params
    remote_server_uri = sys.argv[1]
    experiment_name = sys.argv[2]
    run_name = sys.argv[3]
    run_pipeline(remote_server_uri, experiment_name, run_name)


# remote_server_uri = "https://projet-slums-detection-807277.user.lab.sspcloud.fr"
# experiment_name = "segmentation"
# run_name = "testclem"

# TO DO :
# préparer Test exemples
# indicateur nombre de zones détectées dans l'image
# IOU
# visu
# test routine sur S2Looking dataset

# diminution du nombre d'images DL : pour test

# import os

# list_data_dir = ["../data/PLEIADES/2022/MARTINIQUE/"],
# "../data/PLEIADES/2022/MARTINIQUE/"]

# len(os.listdir(list_data_dir[0]))
# len(os.listdir(list_data_dir[1]))


# def delete_files_in_dir(dir_path,length_delete):

# Loop through the files and delete them
#    for file in files:
#        file_path = os.path.join(dir_path, file)
#        if os.path.isfile(file_path):
#            os.remove(file_path)


# delete_files_in_dir(list_data_dir[0], 1376)
# delete_files_in_dir(list_data_dir[1], 1350)
# optimisation filtrage
