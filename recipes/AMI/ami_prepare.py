"""
Data preparation.

Download: http://groups.inf.ed.ac.uk/ami/download/

Prepares csv from manual annotations "segments/" using RTTM format
"""

import os
import csv
import logging
import glob
import random

from speechbrain.data_io.data_io import (
    read_wav_soundfile,
    load_pkl,
    save_pkl,
)

logger = logging.getLogger(__name__)
OPT_FILE = "opt_ami_prepare.pkl"
TRAIN_CSV = "train.csv"
DEV_CSV = "dev.csv"
TEST_CSV = "test.csv"
SAMPLERATE = 16000

# have to update


def prepare_ami(
    data_folder,
    save_folder,
    splits=["train", "dev", "test"],
    split_ratio=[90, 10],
    seg_dur=300,
    vad=False,
    rand_seed=1234,
):
    """
    Prepares the csv files for the AMI dataset.

    Arguments
    ---------
    data_folder : str
        Path to the folder where the original VoxCeleb dataset is stored.
    save_folder : str
        The directory where to store the csv files.

    splits : list
        List of splits to prepare from ['train', 'dev']
    split_ratio : list
        List if int for train and validation splits
    seg_dur : int
        Segment duration of a chunk in milliseconds
    vad : bool
        To perform VAD or not
    rand_seed : int
        random seed
    """

    # Create configuration for easily skipping data_preparation stage
    conf = {
        "data_folder": data_folder,
        "splits": splits,
        "split_ratio": split_ratio,
        "save_folder": save_folder,
        "vad": vad,
        "seg_dur": seg_dur,
    }

    if not os.path.exists(save_folder):
        os.makedirs(save_folder)

    # Setting ouput files
    save_opt = os.path.join(save_folder, OPT_FILE)
    save_csv_train = os.path.join(save_folder, TRAIN_CSV)
    save_csv_dev = os.path.join(save_folder, DEV_CSV)
    save_csv_test = os.path.join(save_folder, TEST_CSV)

    # Check if this phase is already done (if so, skip it)
    if skip(splits, save_folder, conf):
        logger.debug("Skipping preparation, completed in previous run.")
        return

    # Additional checks to make sure the data folder contains VoxCeleb data
    if "," in data_folder:
        data_folder = data_folder.replace(" ", "").split(",")
    else:
        data_folder = [data_folder]

    _check_voxceleb1_folders(data_folder, splits)

    msg = "\tCreating csv file for the VoxCeleb1 Dataset.."
    logger.debug(msg)

    # Split data into 90% train and 10% validation (verification split)
    wav_lst_train, wav_lst_dev = _get_utt_split_lists(data_folder, split_ratio)

    # Creating csv file for training data
    if "train" in splits:
        prepare_csv(
            SAMPLERATE, seg_dur, wav_lst_train, save_csv_train,
        )

    if "dev" in splits:
        prepare_csv(
            SAMPLERATE, seg_dur, wav_lst_dev, save_csv_dev,
        )

    # Test can be used for verification
    if "test" in splits:
        prepare_csv_test(data_folder, save_csv_test)

    # Saving options (useful to skip this phase when already done)
    save_pkl(conf, save_opt)


def skip(splits, save_folder, conf):
    """
    Detects if the timit data_preparation has been already done.
    If the preparation has been done, we can skip it.

    Returns
    -------
    bool
        if True, the preparation phase can be skipped.
        if False, it must be done.
    """
    # Checking csv files
    skip = True

    split_files = {
        "train": TRAIN_CSV,
        "dev": DEV_CSV,
        "test": TEST_CSV,
    }
    for split in splits:
        if not os.path.isfile(os.path.join(save_folder, split_files[split])):
            skip = False

    #  Checking saved options
    save_opt = os.path.join(save_folder, OPT_FILE)
    if skip is True:
        if os.path.isfile(save_opt):
            opts_old = load_pkl(save_opt)
            if opts_old == conf:
                skip = True
            else:
                skip = False
        else:
            skip = False

    return skip


def _check_voxceleb1_folders(data_folders, splits):
    """
    Check if the data folder actually contains the Voxceleb1 dataset.

    If it does not, raise an error.

    Returns
    -------
    None

    Raises
    ------
    FileNotFoundError
    """
    for data_folder in data_folders:

        if "train" in splits:
            folder = os.path.join(data_folder, "wav", "id10001")
            if not os.path.exists(folder):
                err_msg = (
                    "the folder %s does not exist (as it is expected in "
                    "the Voxceleb dataset)" % folder
                )
                raise FileNotFoundError(err_msg)

        if "test" in splits:
            folder = os.path.join(data_folder, "wav", "id10270")
            if not os.path.exists(folder):
                err_msg = (
                    "the folder %s does not exist (as it is expected in "
                    "the Voxceleb dataset)" % folder
                )
                raise FileNotFoundError(err_msg)

        folder = os.path.join(data_folder, "meta")
        if not os.path.exists(folder):
            err_msg = (
                "the folder %s does not exist (as it is expected in "
                "the Voxceleb dataset)" % folder
            )
            raise FileNotFoundError(err_msg)


# Used for verification split
def _get_utt_split_lists(data_folders, split_ratio):
    """
    Tot. number of speakers = 1211.
    Splits the audio file list into train and dev.
    This function is useful when using verification split
    """
    train_lst = []
    dev_lst = []

    for data_folder in data_folders:

        # Get test sentences (useful for verification)
        test_lst_file = os.path.join(data_folder, "meta", "veri_test.txt")
        test_lst = [
            line.rstrip("\n").split(" ")[1] for line in open(test_lst_file)
        ]
        test_lst = set(sorted(test_lst))

        test_spks = [snt.split("/")[0] for snt in test_lst]

        # avoid test speakers for train and dev splits
        audio_files_list = []
        path = os.path.join(data_folder, "wav", "**", "*.wav")
        for f in glob.glob(path, recursive=True):
            spk_id = f.split("/wav/")[1].split("/")[0]
            if spk_id not in test_spks:
                audio_files_list.append(f)

        random.shuffle(audio_files_list)
        split = int(0.01 * split_ratio[0] * len(audio_files_list))
        train_snts = audio_files_list[:split]
        dev_snts = audio_files_list[split:]

        train_lst.extend(train_snts)
        dev_lst.extend(dev_snts)

    return train_lst, dev_lst


def _get_chunks(seg_dur, audio_id, audio_duration):
    """
    Returns list of chunks
    """
    num_chunks = int(audio_duration * 100 / seg_dur)  # all in milliseconds

    chunk_lst = [
        audio_id + "_" + str(i * seg_dur) + "_" + str(i * seg_dur + seg_dur)
        for i in range(num_chunks)
    ]

    return chunk_lst


def prepare_csv(
    samplerate, seg_dur, wav_lst, csv_file, vad=False,
):
    """
    Creates the csv file given a list of wav files.

    Arguments
    ---------
    wav_lst : list
        The list of wav files of a given data split.
    csv_file : str
        The path of the output csv file
    vad : bool
        Perform VAD. True or False

    Returns
    -------
    None
    """

    msg = '\t"Creating csv lists in  %s..."' % (csv_file)
    logger.debug(msg)

    csv_output = [
        [
            "ID",
            "duration",
            "wav",
            "wav_format",
            "wav_opts",
            "spk_id",
            "spk_id_format",
            "spk_id_opts",
        ]
    ]

    # For assiging unique ID to each chunk
    my_sep = "--"
    entry = []
    # Processing all the wav files in the list
    for wav_file in wav_lst:

        # Getting sentence and speaker ids
        [spk_id, sess_id, utt_id] = wav_file.split("/")[-3:]
        audio_id = my_sep.join([spk_id, sess_id, utt_id.split(".")[0]])

        # Reading the signal (to retrieve duration in seconds)
        signal = read_wav_soundfile(wav_file)
        audio_duration = signal.shape[0] / samplerate

        uniq_chunks_list = _get_chunks(seg_dur, audio_id, audio_duration)

        for chunk in uniq_chunks_list:
            s, e = chunk.split("_")[-2:]
            start_sample = int(int(s) / 100 * samplerate)
            end_sample = int(int(e) / 100 * samplerate)

            start_stop = (
                "start:" + str(start_sample) + " stop:" + str(end_sample)
            )

            # Composition of the csv_line
            csv_line = [
                chunk,
                str(seg_dur / 100),
                wav_file,
                "wav",
                start_stop,
                spk_id,
                "string",
                " ",
            ]

            entry.append(csv_line)

    # Shuffling at chunk level
    random.shuffle(entry)
    csv_output = csv_output + entry

    # Writing the csv lines
    with open(csv_file, mode="w") as csv_f:
        csv_writer = csv.writer(
            csv_f, delimiter=",", quotechar='"', quoting=csv.QUOTE_MINIMAL
        )

        for line in csv_output:
            csv_writer.writerow(line)

    # Final prints
    msg = "\t%s Sucessfully created!" % (csv_file)
    logger.debug(msg)


def prepare_csv_test(data_folders, csv_file):
    """
    Creates the csv file for test data (useful for verification)

    Arguments
    ---------
    data_folder : str
        Path of the data folders
    csv_file : str
        The path of the output csv file

    Returns
    -------
    None
    """

    msg = '\t"Creating csv lists in  %s..."' % (csv_file)
    logger.debug(msg)

    csv_output = [
        [
            "ID",
            "duration",
            "wav1",
            "wav1_format",
            "wav1_opts",
            "wav2",
            "wav2_format",
            "wav2_opts",
            "lab_verification",
            "lab_verification_format",
            "lab_verification_opts",
        ]
    ]

    entry = []
    cnt = 0
    for data_folder in data_folders:
        test_lst_file = os.path.join(data_folder, "meta", "veri_test.txt")

        for line in open(test_lst_file):
            cnt = cnt + 1
            test_id = "test_" + str(cnt)

            lab_verification = line.split(" ")[0]
            test_1_wav = data_folder + "/wav/" + line.split(" ")[1].rstrip()
            test_2_wav = data_folder + "/wav/" + line.split(" ")[2].rstrip()

            # Reading the signal (to retrieve duration in seconds)
            signal = read_wav_soundfile(test_1_wav)
            audio_duration = signal.shape[0] / SAMPLERATE

            # Composition of the csv_line
            csv_line = [
                test_id,
                audio_duration,
                test_1_wav,
                "wav",
                "",
                test_2_wav,
                "wav",
                "",
                lab_verification,
                "string",
                "",
            ]

            entry.append(csv_line)

    csv_output = csv_output + entry

    # Writing the csv lines
    with open(csv_file, mode="w") as csv_f:
        csv_writer = csv.writer(
            csv_f, delimiter=",", quotechar='"', quoting=csv.QUOTE_MINIMAL
        )
        for line in csv_output:
            csv_writer.writerow(line)
