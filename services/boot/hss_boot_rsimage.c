/*******************************************************************************
 * Copyright 2019-2025 Microchip FPGA Embedded Systems Solutions.
 *
 * SPDX-License-Identifier: MIT
 *
 * MPFS HSS Embedded Software
 *
 */

/*!
 * \file  Redundant Striped Boot Image
 * \brief Optional 3-copy / 3-image striped redundant boot scheme
 *
 * See hss_boot_rsimage.h for the on-storage layout description.  This file is
 * compiled when CONFIG_SERVICE_BOOT_REDUNDANT is selected.  It is completely
 * storage independent: all reads go through the HSS_Storage readBlock
 * operation, so the same reader works over QSPI, MMC, or any other backend
 * that implements struct HSS_Storage.  Which backend uses it is chosen at
 * compile time via CONFIG_SERVICE_BOOT_REDUNDANT_QSPI / _MMC (include
 * selection).
 *
 * Every chunk has its own header and the chunk offsets come from the
 * precalculated HSS_RS_OFF_* constants, so there is no global header that
 * could be corrupted to lose the whole image.
 */

#include "config.h"
#include "hss_types.h"
#include "hss_debug.h"
#include "hss_crc32.h"
#include "hss_boot_rsimage.h"
#include "hss_boot_service.h"
#include "hss_boot_pmp.h"
#include "ddr_service.h"

#include <assert.h>
#include <string.h>

#if IS_ENABLED(CONFIG_SERVICE_BOOT_REDUNDANT)

/* Keep the on-flash layout in lock-step with tools/qspi-rs-image */
_Static_assert(sizeof(struct HSS_RS_ChunkHeader) == 48, "HSS_RS_ChunkHeader size");

/*!
 * \brief Precalculated chunk layout, indexed by [image][copy]
 *
 * The offsets are the HSS_RS_OFF_* constants from the header; the capacity is
 * the fixed, block-aligned chunk size.  Nothing here is read from flash.
 */
static const struct HSS_RS_ChunkInfo {
    uint32_t offset;
    uint32_t capacity;
} HSS_RS_layout[HSS_RS_NUM_IMAGES][HSS_RS_NUM_COPIES] = {
    {   /* uboot */
        { HSS_RS_OFF_UBOOT_0,  HSS_RS_CHUNK_UBOOT_SIZE },
        { HSS_RS_OFF_UBOOT_1,  HSS_RS_CHUNK_UBOOT_SIZE },
        { HSS_RS_OFF_UBOOT_2,  HSS_RS_CHUNK_UBOOT_SIZE },
    },
    {   /* kernel */
        { HSS_RS_OFF_KERNEL_0, HSS_RS_CHUNK_KERNEL_SIZE },
        { HSS_RS_OFF_KERNEL_1, HSS_RS_CHUNK_KERNEL_SIZE },
        { HSS_RS_OFF_KERNEL_2, HSS_RS_CHUNK_KERNEL_SIZE },
    },
    {   /* dtb */
        { HSS_RS_OFF_DTB_0,    HSS_RS_CHUNK_DTB_SIZE },
        { HSS_RS_OFF_DTB_1,    HSS_RS_CHUNK_DTB_SIZE },
        { HSS_RS_OFF_DTB_2,    HSS_RS_CHUNK_DTB_SIZE },
    },
};

static const char *rsImageName_(uint32_t imageId)
{
    switch (imageId) {
    case HSS_RS_IMAGE_UBOOT:  return "uboot";
    case HSS_RS_IMAGE_KERNEL: return "kernel";
    case HSS_RS_IMAGE_DTB:    return "dtb";
    default:                  return "unknown";
    }
}

/*!
 * \brief Read and validate one chunk header
 *
 * A corrupt header is not fatal: the caller simply tries the next copy.
 */
/*
 * Storage-independent read helper.
 *
 * MMC requires a sector-aligned source offset (and a 4-byte aligned
 * destination), but the chunk header sits at the chunk start while the payload
 * follows *immediately* after it, so payload reads are not sector aligned.
 * Read the leading partial block through a bounce buffer, then read the now
 * aligned remainder straight into the destination.  QSPI does not require
 * alignment but works fine through the same path.
 */
static uint32_t rsBlockSize_ = 1u;

/* Bounce buffer for partial-block reads.  Must be static/global rather than on
 * the stack: HSS_MMC_ReadBlock moves data with the PDMA engine, which cannot
 * target the E51 stack. */
static uint8_t rsBounce_[4096] __attribute__((aligned(8)));

static bool rsRead_(struct HSS_Storage *pStorage, void *pDest,
    size_t srcOffset, size_t byteCount)
{
    uint8_t *pDestU8 = (uint8_t *)pDest;
    const uint32_t blockSize = rsBlockSize_ ? rsBlockSize_ : 1u;
    uint8_t * const bounce = rsBounce_;

    if (blockSize > sizeof(rsBounce_)) {
        return false;
    }

    size_t off = srcOffset;
    size_t remaining = byteCount;

    while (remaining) {
        const size_t blkOff = off % blockSize;
        const size_t firstLen = (remaining < (blockSize - blkOff))
            ? remaining : (blockSize - blkOff);

        if ((blkOff == 0u) && (remaining >= blockSize)) {
            /* whole aligned blocks: read straight into the destination */
            const size_t fullLen = (remaining / blockSize) * blockSize;
            if (!pStorage->readBlock(pDestU8, off, fullLen)) {
                return false;
            }
            pDestU8 += fullLen;
            off += fullLen;
            remaining -= fullLen;
        } else {
            /* partial block (incl. reads smaller than one block, such as the
             * 48-byte chunk header): read a whole block into bounce and copy
             * out the wanted range.  HSS_MMC_ReadBlock does not cope with a
             * sub-sector transfer on its own. */
            if (!pStorage->readBlock(bounce, off - blkOff, blockSize)) {
                return false;
            }
            memcpy(pDestU8, &bounce[blkOff], firstLen);
            pDestU8 += firstLen;
            off += firstLen;
            remaining -= firstLen;
        }
    }

    return true;
}

static bool rsReadChunkHeader_(struct HSS_Storage *pStorage,
    const struct HSS_RS_ChunkInfo * const pInfo,
    uint32_t image, uint32_t copy, struct HSS_RS_ChunkHeader * const pHeader)
{
    if (!rsRead_(pStorage, pHeader, pInfo->offset, sizeof(*pHeader))) {
        mHSS_DEBUG_PRINTF(LOG_WARN, "RS: %s copy %u header read failed\n",
            rsImageName_(image), copy);
        return false;
    }

    if (pHeader->magic != HSS_RS_CHUNK_MAGIC) {
        mHSS_DEBUG_PRINTF(LOG_WARN,
            "RS: %s copy %u bad chunk magic 0x%08x\n",
            rsImageName_(image), copy, pHeader->magic);
        return false;
    }

    if (pHeader->version != HSS_RS_VERSION) {
        mHSS_DEBUG_PRINTF(LOG_WARN,
            "RS: %s copy %u unsupported chunk version %u\n",
            rsImageName_(image), copy, pHeader->version);
        return false;
    }

    if ((pHeader->imageId != image) || (pHeader->copyIndex != copy) ||
        (pHeader->flashOffset != pInfo->offset)) {
        mHSS_DEBUG_PRINTF(LOG_WARN,
            "RS: %s copy %u chunk header mismatch "
            "(id %u/%u, copy %u/%u, offset 0x%llx/0x%x)\n",
            rsImageName_(image), copy,
            pHeader->imageId, image, pHeader->copyIndex, copy,
            (unsigned long long)pHeader->flashOffset, pInfo->offset);
        return false;
    }

    if ((pHeader->ownerHart < HSS_HART_U54_1) ||
        (pHeader->ownerHart >= HSS_HART_NUM_PEERS)) {
        mHSS_DEBUG_PRINTF(LOG_WARN,
            "RS: %s copy %u invalid owner hart %u\n",
            rsImageName_(image), copy, pHeader->ownerHart);
        return false;
    }

    if (!pHeader->payloadSize ||
        (pHeader->payloadSize > (pInfo->capacity - sizeof(*pHeader)))) {
        mHSS_DEBUG_PRINTF(LOG_WARN,
            "RS: %s copy %u payload 0x%x does not fit capacity 0x%x\n",
            rsImageName_(image), copy, pHeader->payloadSize, pInfo->capacity);
        return false;
    }

    struct HSS_RS_ChunkHeader shadow = *pHeader;
    shadow.headerCrc = 0u;

    const uint32_t headerCrc =
        CRC32_calculate((const uint8_t *)&shadow, sizeof(shadow));

    if (headerCrc != pHeader->headerCrc) {
        mHSS_DEBUG_PRINTF(LOG_WARN,
            "RS: %s copy %u chunk header CRC mismatch "
            "(calculated 0x%08x vs expected 0x%08x)\n",
            rsImageName_(image), copy, headerCrc, pHeader->headerCrc);
        return false;
    }

    return true;
}

/*!
 * \brief Stage one image, verifying its header and payload CRC per copy
 */
static bool rsStageImage_(struct HSS_Storage *pStorage, uint32_t image)
{
    bool found = false;

    for (uint32_t copy = 0u; (copy < HSS_RS_NUM_COPIES) && !found; copy++) {
        const struct HSS_RS_ChunkInfo * const pInfo = &HSS_RS_layout[image][copy];
        struct HSS_RS_ChunkHeader header;

        if (!rsReadChunkHeader_(pStorage, pInfo, image, copy, &header)) {
            continue;
        }

        const uintptr_t dest = (image == HSS_RS_IMAGE_UBOOT)
            ? (uintptr_t)CONFIG_SERVICE_BOOT_DDR_TARGET_ADDR
            : (uintptr_t)header.execAddr;

        if (!HSS_DDR_IsAddrInDDR(dest) ||
            !HSS_DDR_IsAddrInDDR(dest + header.payloadSize) ||
            !HSS_PMP_CheckWrite((enum HSSHartId)header.ownerHart,
                (ptrdiff_t)dest, header.payloadSize)) {
            mHSS_DEBUG_PRINTF(LOG_ERROR,
                "RS: %s copy %u destination 0x%lx/%u not writable by u54_%u\n",
                rsImageName_(image), copy, (unsigned long)dest,
                header.payloadSize, header.ownerHart);
            continue;
        }

        mHSS_DEBUG_PRINTF(LOG_NORMAL,
            "RS: %s copy %u @ 0x%x (%u bytes) -> 0x%lx\n",
            rsImageName_(image), copy, pInfo->offset,
            header.payloadSize, (unsigned long)dest);

        if (!rsRead_(pStorage, (void *)dest,
                pInfo->offset + sizeof(header), header.payloadSize)) {
            mHSS_DEBUG_PRINTF(LOG_WARN, "RS: %s copy %u payload read failed\n",
                rsImageName_(image), copy);
            continue;
        }

        const uint32_t crc = CRC32_calculate((const uint8_t *)dest, header.payloadSize);
        if (crc == header.payloadCrc) {
            mHSS_DEBUG_PRINTF(LOG_NORMAL,
                "RS: %s copy %u CRC OK (0x%08x)\n",
                rsImageName_(image), copy, crc);
            found = true;
        } else {
            mHSS_DEBUG_PRINTF(LOG_WARN,
                "RS: %s copy %u CRC mismatch "
                "(calculated 0x%08x vs expected 0x%08x)\n",
                rsImageName_(image), copy, crc, header.payloadCrc);
        }
    }

    if (!found) {
        mHSS_DEBUG_PRINTF(LOG_ERROR,
            "RS: all %u copies of \"%s\" failed\n",
            HSS_RS_NUM_COPIES, rsImageName_(image));
    }

    return found;
}

bool HSS_Boot_GetRedundantImage(struct HSS_Storage *pStorage,
    struct HSS_BootImage **ppBootImage)
{
    assert(pStorage);
    assert(ppBootImage);

    uint32_t pageSize = 0u, eraseSize = 0u, pageCount = 0u;

    if (!pStorage->readBlock) {
        mHSS_DEBUG_PRINTF(LOG_ERROR,
            "RS: storage \"%s\" has no readBlock operation\n", pStorage->name);
        return false;
    }

    if (pStorage->getInfo) {
        pStorage->getInfo(&pageSize, &eraseSize, &pageCount);
    }
    rsBlockSize_ = pageSize ? pageSize : 1u;

    /* the precalculated layout must fit the actual storage */
    if (eraseSize && pageCount) {
        const uint64_t storageBytes = (uint64_t)eraseSize * pageCount;
        const uint64_t usedBytes = (uint64_t)HSS_RS_OFF_DTB_2 + HSS_RS_CHUNK_DTB_SIZE;

        if (usedBytes > storageBytes) {
            mHSS_DEBUG_PRINTF(LOG_ERROR,
                "RS: layout needs 0x%llx bytes but storage is 0x%llx bytes\n",
                (unsigned long long)usedBytes, (unsigned long long)storageBytes);
            return false;
        }
    }

    mHSS_DEBUG_PRINTF(LOG_NORMAL,
        "RS: storage %s, %u images x %u copies, constant layout, "
        "copy stride 0x%x, first chunk at 0x%x\n",
        pStorage->name,
        HSS_RS_NUM_IMAGES, HSS_RS_NUM_COPIES,
        HSS_RS_COPY_STRIDE, HSS_RS_BOOT_BLOCK);

    /* verify each image's chunk/payload CRC across its copies and stage it */
    for (uint32_t image = 0u; image < HSS_RS_NUM_IMAGES; image++) {
        if (!rsStageImage_(pStorage, image)) {
            return false;
        }
    }

    /* hand the staged uboot copy to the regular boot service */
    struct HSS_BootImage * const pBootImage =
        (struct HSS_BootImage *)(uintptr_t)CONFIG_SERVICE_BOOT_DDR_TARGET_ADDR;

    if (!HSS_Boot_VerifyMagic(pBootImage)) {
        mHSS_DEBUG_PRINTF(LOG_ERROR, "RS: staged uboot image failed magic check\n");
        return false;
    }

    mHSS_DEBUG_PRINTF(LOG_NORMAL, "RS: staged uboot boot image at 0x%p\n",
        pBootImage);
    *ppBootImage = pBootImage;

    return true;
}

#endif /* CONFIG_SERVICE_BOOT_REDUNDANT */
