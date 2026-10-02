/*******************************************************************************
 * Copyright 2019-2026 Microchip FPGA Embedded Systems Solutions.
 *
 * SPDX-License-Identifier: MIT
 *
 * MPFS HSS Embedded Software
 *
 */

/*!
 * \file  Redundant Slot Boot Image
 * \brief N version slots x M copies, whole-blob replication
 *
 * See docs/DESIGN-redundant-slot-boot.md and hss_boot_slotimage.h.  This file
 * is compiled when CONFIG_SERVICE_BOOT_REDUNDANT is selected.  It is
 * storage independent: every read goes through HSS_Storage readBlock, so the
 * same walker works over QSPI, MMC or any other backend.
 *
 * Nothing here is read from flash to find a copy: region offsets are the
 * compile-time constants from hss_boot_slotimage.h.
 */

#include "config.h"
#include "hss_types.h"
#include "hss_debug.h"
#include "hss_crc32.h"
#include "hss_boot_slotimage.h"
#include "hss_boot_service.h"
#include "hss_boot_pmp.h"
#include "ddr_service.h"

#include <assert.h>
#include <string.h>

#if IS_ENABLED(CONFIG_SERVICE_BOOT_REDUNDANT)

_Static_assert(sizeof(struct HSS_SlotCopyHeader) == HSS_SLOT_HDR_SIZE,
    "HSS_SlotCopyHeader size");

/*
 * Storage-independent read helper.
 *
 * MMC requires a sector-aligned source offset (and a 4-byte aligned
 * destination), but a copy header sits at the region start while the payload
 * follows *immediately* after it, so payload reads are not sector aligned.
 * Read the leading partial block through a bounce buffer, then the now
 * aligned remainder straight into the destination.  QSPI needs no alignment
 * but works fine through the same path.
 */
static uint32_t slotBlockSize_ = 1u;

/* Bounce buffer for partial-block reads.  Must be static/global rather than on
 * the stack: HSS_MMC_ReadBlock moves data with the PDMA engine, which cannot
 * target the E51 stack. */
static uint8_t slotBounce_[4096] __attribute__((aligned(8)));

static bool slotRead_(struct HSS_Storage *pStorage, void *pDest,
    size_t srcOffset, size_t byteCount)
{
    uint8_t *pDestU8 = (uint8_t *)pDest;
    const uint32_t blockSize = slotBlockSize_ ? slotBlockSize_ : 1u;
    uint8_t * const bounce = slotBounce_;

    if (blockSize > sizeof(slotBounce_)) {
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
             * 48-byte header): read a whole block into bounce and copy out the
             * wanted range.  HSS_MMC_ReadBlock does not cope with a sub-sector
             * transfer on its own. */
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

/*!
 * \brief Read and validate one copy header
 *
 * A corrupt header is not fatal: the caller tries the next copy.
 */
static bool slotReadHeader_(struct HSS_Storage *pStorage, size_t offset,
    uint32_t slot, uint32_t copy, struct HSS_SlotCopyHeader * const pHeader)
{
    assert(pHeader);

    if (!slotRead_(pStorage, pHeader, offset, sizeof(*pHeader))) {
        mHSS_DEBUG_PRINTF(LOG_WARN,
            "SLOT: slot %u copy %u header read failed\n", slot, copy);
        return false;
    }

    if (pHeader->magic != HSS_SLOT_MAGIC) {
        mHSS_DEBUG_PRINTF(LOG_WARN,
            "SLOT: slot %u copy %u bad header magic 0x%08x\n",
            slot, copy, pHeader->magic);
        return false;
    }

    if (pHeader->version != HSS_SLOT_VERSION) {
        mHSS_DEBUG_PRINTF(LOG_WARN,
            "SLOT: slot %u copy %u unsupported version %u\n",
            slot, copy, pHeader->version);
        return false;
    }

    if ((pHeader->slotId != slot) || (pHeader->copyIndex != copy) ||
        (pHeader->flashOffset != (uint64_t)offset)) {
        mHSS_DEBUG_PRINTF(LOG_WARN,
            "SLOT: slot %u copy %u header mismatch "
            "(slotId %u copyIndex %u offset 0x%llx/0x%lx)\n",
            slot, copy, pHeader->slotId, pHeader->copyIndex,
            (unsigned long long)pHeader->flashOffset, (unsigned long)offset);
        return false;
    }

    if (((enum HSSHartId)pHeader->ownerHart < HSS_HART_U54_1) ||
        ((enum HSSHartId)pHeader->ownerHart >= HSS_HART_NUM_PEERS)) {
        mHSS_DEBUG_PRINTF(LOG_WARN,
            "SLOT: slot %u copy %u invalid owner hart %u\n",
            slot, copy, pHeader->ownerHart);
        return false;
    }

    if (!pHeader->payloadSize ||
        (pHeader->payloadSize > (HSS_SLOT_CAPACITY - HSS_SLOT_HDR_SIZE))) {
        mHSS_DEBUG_PRINTF(LOG_WARN,
            "SLOT: slot %u copy %u payload 0x%x does not fit capacity 0x%x\n",
            slot, copy, pHeader->payloadSize, HSS_SLOT_CAPACITY);
        return false;
    }

    struct HSS_SlotCopyHeader shadow = *pHeader;
    shadow.headerCrc = 0u;

    const uint32_t headerCrc =
        CRC32_calculate((const uint8_t *)&shadow, sizeof(shadow));

    if (headerCrc != pHeader->headerCrc) {
        mHSS_DEBUG_PRINTF(LOG_WARN,
            "SLOT: slot %u copy %u header CRC mismatch "
            "(calculated 0x%08x vs expected 0x%08x)\n",
            slot, copy, headerCrc, pHeader->headerCrc);
        return false;
    }

    return true;
}

/*!
 * \brief Walk the slots and copies, staging the first valid blob
 */
bool HSS_Boot_GetSlotImage(struct HSS_Storage *pStorage,
    struct HSS_BootImage **ppBootImage)
{
    assert(pStorage);
    assert(ppBootImage);

    if (!pStorage->readBlock) {
        mHSS_DEBUG_PRINTF(LOG_ERROR,
            "SLOT: storage \"%s\" has no readBlock operation\n", pStorage->name);
        return false;
    }

    uint32_t pageSize = 0u, eraseSize = 0u, pageCount = 0u;

    if (pStorage->getInfo) {
        pStorage->getInfo(&pageSize, &eraseSize, &pageCount);
    }
    slotBlockSize_ = pageSize ? pageSize : 1u;

    /* the constant layout must fit the actual storage */
    if (pageSize && pageCount) {
        /* total bytes = blockSize * blockCount (first x third getInfo param).
         * For MMC eraseSize == blockSize, but for QSPI eraseSize is the erase
         * unit while blockCount is a count of 512-byte sectors, so their
         * product is meaningless. */
        const uint64_t storageBytes = (uint64_t)pageSize * pageCount;

        if ((uint64_t)HSS_SLOT_LAYOUT_END > storageBytes) {
            mHSS_DEBUG_PRINTF(LOG_ERROR,
                "SLOT: layout needs 0x%llx bytes but storage is 0x%llx bytes\n",
                (unsigned long long)HSS_SLOT_LAYOUT_END,
                (unsigned long long)storageBytes);
            return false;
        }
    }

    mHSS_DEBUG_PRINTF(LOG_NORMAL,
        "SLOT: storage %s, %u slots x %u copies, base 0x%x capacity 0x%x "
        "stride 0x%x, layout end 0x%x\n",
        pStorage->name, HSS_SLOT_COUNT, HSS_SLOT_COPIES,
        HSS_SLOT_BASE, HSS_SLOT_CAPACITY, HSS_SLOT_STRIDE, HSS_SLOT_LAYOUT_END);

    const uintptr_t dest = (uintptr_t)CONFIG_SERVICE_BOOT_DDR_TARGET_ADDR;

    for (uint32_t slot = 0u; slot < HSS_SLOT_COUNT; slot++) {
        for (uint32_t copy = 0u; copy < HSS_SLOT_COPIES; copy++) {
            const size_t offset = HSS_SLOT_REGION(slot, copy);
            struct HSS_SlotCopyHeader header;

            if (!slotReadHeader_(pStorage, offset, slot, copy, &header)) {
                continue;
            }

            if (!HSS_DDR_IsAddrInDDR(dest) ||
                !HSS_DDR_IsAddrInDDR(dest + header.payloadSize - 1u) ||
                !HSS_PMP_CheckWrite((enum HSSHartId)header.ownerHart,
                    (ptrdiff_t)dest, header.payloadSize)) {
                mHSS_DEBUG_PRINTF(LOG_WARN,
                    "SLOT: slot %u copy %u destination 0x%lx/%u not writable "
                    "by u54_%u\n",
                    slot, copy, (unsigned long)dest, header.payloadSize,
                    header.ownerHart);
                continue;
            }

            if (!slotRead_(pStorage, (void *)dest,
                    offset + HSS_SLOT_HDR_SIZE, header.payloadSize)) {
                mHSS_DEBUG_PRINTF(LOG_WARN,
                    "SLOT: slot %u copy %u payload read failed\n", slot, copy);
                continue;
            }

            const uint32_t payloadCrc =
                CRC32_calculate((const uint8_t *)dest, header.payloadSize);

            if (payloadCrc != header.payloadCrc) {
                mHSS_DEBUG_PRINTF(LOG_WARN,
                    "SLOT: slot %u copy %u payload CRC mismatch "
                    "(calculated 0x%08x vs expected 0x%08x)\n",
                    slot, copy, payloadCrc, header.payloadCrc);
                continue;
            }

            struct HSS_BootImage * const pBootImage =
                (struct HSS_BootImage *)dest;

            if (!HSS_Boot_VerifyMagic(pBootImage)) {
                mHSS_DEBUG_PRINTF(LOG_WARN,
                    "SLOT: slot %u copy %u staged blob is not a valid boot "
                    "image\n", slot, copy);
                continue;
            }

            if (pBootImage->bootImageLength > header.payloadSize) {
                mHSS_DEBUG_PRINTF(LOG_WARN,
                    "SLOT: slot %u copy %u blob length 0x%lx exceeds staged "
                    "payload 0x%x\n",
                    slot, copy, (unsigned long)pBootImage->bootImageLength,
                    header.payloadSize);
                continue;
            }

            mHSS_DEBUG_PRINTF(LOG_NORMAL,
                "SLOT: slot %c copy %u OK (contentVersion %u, %u bytes)\n",
                'A' + slot, copy, header.contentVersion, header.payloadSize);

            *ppBootImage = pBootImage;
            return true;
        }

        mHSS_DEBUG_PRINTF(LOG_ERROR,
            "SLOT: slot %c: all %u copies rejected\n",
            'A' + slot, HSS_SLOT_COPIES);
    }

    mHSS_DEBUG_PRINTF(LOG_ERROR, "SLOT: all slots exhausted\n");

    return false;
}

#endif /* CONFIG_SERVICE_BOOT_REDUNDANT */
