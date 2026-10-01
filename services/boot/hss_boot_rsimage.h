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
 * This scheme is an alternative to a storage's normal boot image lookup.
 * It is enabled by CONFIG_SERVICE_BOOT_REDUNDANT and is storage independent:
 * the reader uses the HSS_Storage readBlock operation.  Which storage backend
 * applies it is selected at compile time with CONFIG_SERVICE_BOOT_REDUNDANT_QSPI
 * or CONFIG_SERVICE_BOOT_REDUNDANT_MMC.  When disabled, each storage keeps its
 * original boot path.
 *
 * The storage is divided into nine chunks.  Three images - the HSS uboot
 * boot image, a Linux kernel Image and a device tree blob - are each stored
 * as three redundant copies.  The chunks are striped by image, so the copies
 * of any one image are spread across the storage:
 *
 *   +---------+---------+---------+---------+---------+---------+---------+---------+---------+
 *   | uboot 0 | kernel0 | dtb  0  | uboot 1 | kernel1 | dtb  1  | uboot 2 | kernel2 | dtb  2  |
 *   +---------+---------+---------+---------+---------+---------+---------+---------+---------+
 *
 * Every chunk carries its own header (magic, image id, copy index, payload
 * size, payload CRC and header CRC).  There is no single global header, so a
 * corrupt header can only ever affect the one copy it belongs to; the HSS
 * moves on to the next copy.
 *
 * The chunk offsets are *precalculated constants* (see the HSS_RS_OFF_*
 * definitions below), so the HSS never has to read anything to find a chunk.
 * The fixed chunk capacities are rounded up from the expected image sizes;
 * the actual payload size is recorded in each chunk header.
 *
 * The layout constants below can either be taken from the committed defaults,
 * or generated from the actual image sizes by tools/qspi-rs-image with
 * `--auto-layout --emit-header`.  A generated hss_boot_rsimage_layout.h (same
 * directory) takes precedence; without one the defaults below are used so a
 * fresh checkout always builds.
 *
 * tools/qspi-rs-image also parses these constants when building an image, so
 * the firmware and the image builder cannot drift apart.
 */

#ifndef HSS_BOOT_RSIMAGE_H
#define HSS_BOOT_RSIMAGE_H

#include "hss_types.h"

#define HSS_RS_CHUNK_MAGIC (0x52534348u) /* 'R','S','C','H' */
#define HSS_RS_VERSION     (2u)

#define HSS_RS_NUM_IMAGES  (3u)
#define HSS_RS_NUM_COPIES  (3u)
#define HSS_RS_NUM_CHUNKS  (HSS_RS_NUM_IMAGES * HSS_RS_NUM_COPIES)

/* Generated layout (--auto-layout --emit-header) wins over the defaults. */
#if defined(__has_include)
#  if __has_include("hss_boot_rsimage_layout.h")
#    include "hss_boot_rsimage_layout.h"
#  endif
#endif

#ifndef HSS_RS_BOOT_BLOCK
/*
 * Precalculated, constant storage layout.
 *
 * The first chunk starts at HSS_RS_BOOT_BLOCK (the block before it is left
 * unused).  Each chunk capacity is a fixed, block-aligned constant sized for
 * the expected image; the real payload length lives in the chunk header.
 */
#  define HSS_RS_BOOT_BLOCK        (0x10000u)  /* 64 KiB */
#  define HSS_RS_CHUNK_UBOOT_SIZE  (0x100000u) /* 1 MiB  */
#  define HSS_RS_CHUNK_KERNEL_SIZE (0x300000u) /* 3 MiB  */
#  define HSS_RS_CHUNK_DTB_SIZE    (0x10000u)  /* 64 KiB */

#  define HSS_RS_COPY_STRIDE \
      (HSS_RS_CHUNK_UBOOT_SIZE + HSS_RS_CHUNK_KERNEL_SIZE + HSS_RS_CHUNK_DTB_SIZE)

/* absolute, precalculated chunk offsets (striped by image) */
#  define HSS_RS_OFF_UBOOT_0  (HSS_RS_BOOT_BLOCK)
#  define HSS_RS_OFF_KERNEL_0 (HSS_RS_OFF_UBOOT_0 + HSS_RS_CHUNK_UBOOT_SIZE)
#  define HSS_RS_OFF_DTB_0    (HSS_RS_OFF_KERNEL_0 + HSS_RS_CHUNK_KERNEL_SIZE)

#  define HSS_RS_OFF_UBOOT_1  (HSS_RS_OFF_UBOOT_0 + HSS_RS_COPY_STRIDE)
#  define HSS_RS_OFF_KERNEL_1 (HSS_RS_OFF_KERNEL_0 + HSS_RS_COPY_STRIDE)
#  define HSS_RS_OFF_DTB_1    (HSS_RS_OFF_DTB_0 + HSS_RS_COPY_STRIDE)

#  define HSS_RS_OFF_UBOOT_2  (HSS_RS_OFF_UBOOT_0 + 2u * HSS_RS_COPY_STRIDE)
#  define HSS_RS_OFF_KERNEL_2 (HSS_RS_OFF_KERNEL_0 + 2u * HSS_RS_COPY_STRIDE)
#  define HSS_RS_OFF_DTB_2    (HSS_RS_OFF_DTB_0 + 2u * HSS_RS_COPY_STRIDE)
#endif /* !HSS_RS_BOOT_BLOCK */

/*!
 * \brief Logical image identifiers in the striped layout
 */
enum HSS_RS_ImageId {
    HSS_RS_IMAGE_UBOOT = 0,  /*!< HSS-formatted boot image (OpenSBI + U-Boot) */
    HSS_RS_IMAGE_KERNEL = 1, /*!< Linux kernel Image                          */
    HSS_RS_IMAGE_DTB = 2,    /*!< Linux device tree blob                      */
};

/*!
 * \brief Per-chunk header, stored at the start of every chunk
 *
 * There is one of these per chunk, so corruption of a header only loses the
 * copy it belongs to.  headerCrc covers the structure with headerCrc zeroed.
 */
struct HSS_RS_ChunkHeader {
    uint32_t magic;       /*!< HSS_RS_CHUNK_MAGIC                        */
    uint32_t version;     /*!< HSS_RS_VERSION                            */
    uint32_t imageId;     /*!< enum HSS_RS_ImageId                       */
    uint32_t copyIndex;   /*!< redundant copy index (0..numCopies-1)     */
    uint32_t payloadSize; /*!< payload size in bytes                     */
    uint32_t payloadCrc;  /*!< CRC32 of the payload                      */
    uint32_t headerCrc;   /*!< CRC32 of this header, headerCrc == 0      */
    uint32_t ownerHart;   /*!< enum HSSHartId permission subject (U54)   */
    uint64_t execAddr;    /*!< DDR address the valid copy is staged to   */
    uint64_t flashOffset; /*!< absolute storage offset of this chunk      */
};

/*!
 * \brief Read and validate a redundant striped boot image from storage
 *
 * Walks the constant chunk layout, validates each chunk header, verifies the
 * payload CRC (trying all copies), stages every image to its exec address and
 * returns the staged HSS boot image (the uboot copy) via ppBootImage.
 */
bool HSS_Boot_GetRedundantImage(struct HSS_Storage *pStorage,
    struct HSS_BootImage **ppBootImage);

#endif /* HSS_BOOT_RSIMAGE_H */
