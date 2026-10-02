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
 * See docs/DESIGN-redundant-slot-boot.md.  Flash holds N slots (bootable
 * alternatives - two versions of one slot type) each replicated M times.
 * Bands are copy-major, so copies of a slot are a full stride apart:
 *
 *   region(s,k) = base + k*stride + s*c     stride = N*c
 *
 * HSS walks slots in priority order, validates each copy by header CRC and
 * payload CRC32, stages the first good one at
 * CONFIG_SERVICE_BOOT_DDR_TARGET_ADDR and hands it to the stock boot service.
 * The blob is a stock HSS_BootImage, so hss_boot_service.c is untouched.
 *
 * The reader is storage independent: all reads go through HSS_Storage.
 *
 * Layout constants come from the committed defaults below, or from a
 * generated hss_slot_layout.h (same directory) which takes precedence.  The
 * generator parses the same constants back, so firmware and images cannot
 * drift.
 */

#ifndef HSS_BOOT_SLOTIMAGE_H
#define HSS_BOOT_SLOTIMAGE_H

#include "hss_types.h"

#define HSS_SLOT_MAGIC    (0x534C4F54u) /* 'S','L','O','T' */
#define HSS_SLOT_VERSION  (3u)
#define HSS_SLOT_HDR_SIZE (48u)

/*!
 * \brief On-flash copy header, one per region (48 bytes, little-endian)
 *
 * headerCrc covers the structure with headerCrc zeroed.  There is no global
 * header, so a damaged header costs exactly one copy.  There is deliberately
 * no type field - firmware is type-blind; the blob's chunk table says what to
 * run.
 */
struct HSS_SlotCopyHeader {
    uint32_t magic;          /* HSS_SLOT_MAGIC                            */
    uint32_t version;        /* HSS_SLOT_VERSION                          */
    uint32_t slotId;         /* version slot index, build-time order      */
    uint32_t copyIndex;      /* replica index                             */
    uint32_t payloadSize;    /* authoritative blob length                 */
    uint32_t payloadCrc;     /* CRC32 of blob                             */
    uint32_t headerCrc;      /* CRC32 of this struct, headerCrc == 0      */
    uint32_t ownerHart;      /* U54 PMP subject for the staged blob       */
    uint32_t contentVersion; /* monotonic build id, field diagnostics     */
    uint32_t reserved0;      /* 0                                         */
    uint64_t flashOffset;    /* absolute offset of this region            */
};

_Static_assert(sizeof(struct HSS_SlotCopyHeader) == HSS_SLOT_HDR_SIZE,
    "HSS_SlotCopyHeader must be 48 bytes");

/* Generated layout (--emit-header) wins over the committed defaults. */
#if defined(__has_include)
#  if __has_include("hss_slot_layout.h")
#    include "hss_slot_layout.h"
#  endif
#endif

#ifndef HSS_SLOT_BASE
/*!
 * \brief Production defaults: MT25QU02G, 256 MiB
 *
 * base = 0x10000, c = 34 MiB, N = 2, M = 3, stride = 68 MiB, gap = 34 MiB,
 * layout end = 0xCC10000 (204.06 MiB, 20.3 % free).  All offsets 64 KiB
 * aligned.  Frozen once authored: c is baked into eNVM.
 */
#  define HSS_SLOT_BASE      (0x0010000u)
#  define HSS_SLOT_CAPACITY  (0x2200000u) /* 34 MiB */
#  define HSS_SLOT_COUNT     (2u)         /* N */
#  define HSS_SLOT_COPIES    (3u)         /* M */
#endif

#define HSS_SLOT_STRIDE      (HSS_SLOT_COUNT * HSS_SLOT_CAPACITY)
#define HSS_SLOT_GAP         ((HSS_SLOT_COUNT - 1u) * HSS_SLOT_CAPACITY)
#define HSS_SLOT_LAYOUT_END  (HSS_SLOT_BASE + HSS_SLOT_COPIES * HSS_SLOT_STRIDE)

/*!
 * \brief Byte offset of region (slot, copy)
 *
 * Constant: never read back from flash.
 */
#define HSS_SLOT_REGION(slot, copy) \
    (HSS_SLOT_BASE + (copy) * HSS_SLOT_STRIDE + (slot) * HSS_SLOT_CAPACITY)

/*!
 * \brief Read, validate and stage a redundant slot image from storage
 *
 * Walks slots in priority order and copies within each slot, validates each
 * copy header, verifies the payload CRC, stages the first good blob to
 * CONFIG_SERVICE_BOOT_DDR_TARGET_ADDR and returns it via ppBootImage.
 */
bool HSS_Boot_GetSlotImage(struct HSS_Storage *pStorage,
    struct HSS_BootImage **ppBootImage);

#endif /* HSS_BOOT_SLOTIMAGE_H */
