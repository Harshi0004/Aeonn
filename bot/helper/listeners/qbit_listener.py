from time import time
from asyncio import sleep

from bot import (
    LOGGER,
    QbInterval,
    QbTorrents,
    bot_loop,
    config_dict,
    xnox_client,
    download_dict,
    qb_listener_lock,
    download_dict_lock,
)
from bot.helper.ext_utils.bot_utils import (
    new_task,
    sync_to_async,
    getDownloadByGid,
    get_readable_time,
)
from bot.helper.ext_utils.files_utils import clean_unwanted
from bot.helper.ext_utils.task_manager import limit_checker, stop_duplicate_check
from bot.helper.telegram_helper.message_utils import update_all_messages
from bot.helper.mirror_leech_utils.status_utils.qbit_status import QbittorrentStatus


async def __remove_torrent(hash_, tag):
    await sync_to_async(
        xnox_client.torrents_delete, torrent_hashes=hash_, delete_files=True
    )
    async with qb_listener_lock:
        if tag in QbTorrents:
            del QbTorrents[tag]
    await sync_to_async(xnox_client.torrents_delete_tags, tags=tag)


@new_task
async def __onDownloadError(err, tor, button=None):
    LOGGER.info(f"Cancelling Download: {tor.name}")
    ext_hash = tor.hash
    download = await getDownloadByGid(ext_hash[:8])
    listener = download.listener()
    await listener.onDownloadError(err, button)
    await sync_to_async(xnox_client.torrents_pause, torrent_hashes=ext_hash)
    await sleep(0.3)
    await __remove_torrent(ext_hash, tor.tags)


@new_task
async def __onSeedFinish(tor):
    ext_hash = tor.hash
    LOGGER.info(f"Cancelling Seed: {tor.name}")
    download = await getDownloadByGid(ext_hash[:8])
    if not hasattr(download, "seeders_num"):
        return
    listener = download.listener()
    msg = f"Seeding stopped with Ratio: {round(tor.ratio, 3)} and Time: {get_readable_time(tor.seeding_time, True)}"
    await listener.onUploadError(msg)
    await __remove_torrent(ext_hash, tor.tags)


@new_task
async def __stop_duplicate(tor):
    download = await getDownloadByGid(tor.hash[:8])
    if not hasattr(download, "listener"):
        return
    listener = download.listener()
    name = tor.content_path.rsplit("/", 1)[-1].rsplit(".!qB", 1)[0]
    msg, button = await stop_duplicate_check(name, listener)
    if msg:
        __onDownloadError(msg, tor, button)


@new_task
async def __size_checked(tor):
    download = await getDownloadByGid(tor.hash[:8])
    if hasattr(download, "listener"):
        listener = download.listener()
        size = tor.size
        if limit_exceeded := await limit_checker(size, listener, True):
            await __onDownloadError(limit_exceeded, tor)


@new_task
async def __onDownloadComplete(tor):
    ext_hash = tor.hash
    tag = tor.tags
    await sleep(2)
    download = await getDownloadByGid(ext_hash[:8])
    listener = download.listener()
    if not listener.seed:
        await sync_to_async(xnox_client.torrents_pause, torrent_hashes=ext_hash)
    if listener.select:
        await clean_unwanted(listener.dir)
    await listener.onDownloadComplete()
    if listener.seed:
        async with download_dict_lock:
            if listener.uid in download_dict:
                removed = False
                download_dict[listener.uid] = QbittorrentStatus(listener, True)
            else:
                removed = True
        if removed:
            await __remove_torrent(ext_hash, tag)
            return
        async with qb_listener_lock:
            if tag in QbTorrents:
                QbTorrents[tag]["seeding"] = True
            else:
                return
        await update_all_messages()
        LOGGER.info(f"Seeding started: {tor.name} - Hash: {ext_hash}")
    else:
        await __remove_torrent(ext_hash, tag)


async def __qb_listener():
    while True:
        async with qb_listener_lock:
            try:
                if len(await sync_to_async(xnox_client.torrents_info)) == 0:
                    QbInterval.clear()
                    break
                for tor_info in await sync_to_async(xnox_client.torrents_info):
                    tag = tor_info.tags
                    if tag not in QbTorrents:
                        continue
                    state = tor_info.state
                    if state == "metaDL":
                        TORRENT_TIMEOUT = config_dict["TORRENT_TIMEOUT"]
                        QbTorrents[tag]["stalled_time"] = time()
                        if (
                            TORRENT_TIMEOUT
                            and time() - tor_info.added_on >= TORRENT_TIMEOUT
                        ):
                            __onDownloadError("Dead Torrent!", tor_info)
                        else:
                            await sync_to_async(
                                xnox_client.torrents_reannounce,
                                torrent_hashes=tor_info.hash,
                            )
                    elif state == "downloading":
                        QbTorrents[tag]["stalled_time"] = time()
                        if (
                            config_dict["STOP_DUPLICATE"]
                            and not QbTorrents[tag]["stop_dup_check"]
                        ):
                            QbTorrents[tag]["stop_dup_check"] = True
                            __stop_duplicate(tor_info)
                        if not QbTorrents[tag]["size_checked"]:
                            QbTorrents[tag]["size_checked"] = True
                            __size_checked(tor_info)
                    elif state == "stalledDL":
                        TORRENT_TIMEOUT = config_dict["TORRENT_TIMEOUT"]
                        if (
                            not QbTorrents[tag]["rechecked"]
                            and 0.99989999999999999 < tor_info.progress < 1
                        ):
                            msg = f"Force recheck - Name: {tor_info.name} Hash: "
                            msg += f"{tor_info.hash} Downloaded Bytes: {tor_info.downloaded} "
                            msg += f"Size: {tor_info.size} Total Size: {tor_info.total_size}"
                            LOGGER.warning(msg)
                            await sync_to_async(
                                xnox_client.torrents_recheck,
                                torrent_hashes=tor_info.hash,
                            )
                            QbTorrents[tag]["rechecked"] = True
                        elif (
                            TORRENT_TIMEOUT
                            and time() - QbTorrents[tag]["stalled_time"]
                            >= TORRENT_TIMEOUT
                        ):
                            __onDownloadError("Dead Torrent!", tor_info)
                        else:
                            await sync_to_async(
                                xnox_client.torrents_reannounce,
                                torrent_hashes=tor_info.hash,
                            )
                    elif state == "missingFiles":
                        await sync_to_async(
                            xnox_client.torrents_recheck,
                            torrent_hashes=tor_info.hash,
                        )
                    elif state == "error":
                        __onDownloadError(
                            "No enough space for this torrent on device", tor_info
                        )
                    elif (
                        tor_info.completion_on != 0
                        and not QbTorrents[tag]["uploaded"]
                        and state
                        not in ["checkingUP", "checkingDL", "checkingResumeData"]
                    ):
                        QbTorrents[tag]["uploaded"] = True
                        __onDownloadComplete(tor_info)
                    elif (
                        state in ["pausedUP", "pausedDL"]
                        and QbTorrents[tag]["seeding"]
                    ):
                        QbTorrents[tag]["seeding"] = False
                        __onSeedFinish(tor_info)
            except Exception as e:
                LOGGER.error(str(e))
        await sleep(3)


async def onDownloadStart(tag):
    async with qb_listener_lock:
        QbTorrents[tag] = {
            "stalled_time": time(),
            "stop_dup_check": False,
            "rechecked": False,
            "uploaded": False,
            "seeding": False,
            "size_checked": False,
        }
        if not QbInterval:
            periodic = bot_loop.create_task(__qb_listener())
            QbInterval.append(periodic)
