/*
 * qnapled - make the QNAP power LED steady instead of blinking.
 *
 * On the TS-x69 (Granite Well board) the green power LED hangs off pin GP40
 * of the ITE IT8721F Super I/O. The BIOS hands that pin to the chip's
 * "GP LED blinking 1" engine, and QNAP's own OS later takes it back; plain
 * FreeBSD never does, so the LED blinks forever. This detaches the blinker
 * and drives GP40 as a simple-I/O output, low = LED on.
 *
 *   qnapled status   show the relevant registers
 *   qnapled steady   blinker off, GP40 output low (LED steady on)
 *   qnapled blink    put the BIOS setup back (blinker 1 on GP40)
 *
 * Needs root (/dev/io). Does nothing unless an IT8721F is at 0x2E and its
 * blinker 1 is (or was, by us) attached to GP40.
 *
 * Registers, all in logical device 7 (GPIO):
 *   F8  GP LED blinking 1 pin mapping, (set << 3) | bit; 0x20 = GP40
 *   C3  simple-I/O enable, GPIO set 4     CB  output enable, GPIO set 4
 *   62/63  simple-I/O base; base + 3 = set 4 data register
 */
#include <sys/types.h>
#include <machine/cpufunc.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define SIO		0x2e
#define LDN_GPIO	0x07
#define REG_BLINK1_PIN	0xf8
#define REG_SIMPLE_SET4	0xc3
#define REG_OUTPUT_SET4	0xcb
#define GP40_LOCATION	0x20
#define GP40_BIT	0x01

static u_char
sio_read(int reg)
{
	outb(SIO, reg);
	return (inb(SIO + 1));
}

static void
sio_write(int reg, u_char val)
{
	outb(SIO, reg);
	outb(SIO + 1, val);
}

static void
usage(void)
{
	fprintf(stderr, "usage: qnapled status | steady | blink\n");
}

int
main(int argc, char **argv)
{
	const char *cmd;
	u_int base, data;
	u_char pin, simple, output;
	int rc = 0;

	if (argc != 2) {
		usage();
		return (2);
	}
	cmd = argv[1];
	if (strcmp(cmd, "status") && strcmp(cmd, "steady") &&
	    strcmp(cmd, "blink")) {
		usage();
		return (2);
	}
	if (open("/dev/io", O_RDWR) < 0) {
		perror("qnapled: /dev/io");
		return (1);
	}

	/* ITE config-mode entry key for port 0x2E */
	outb(SIO, 0x87); outb(SIO, 0x01); outb(SIO, 0x55); outb(SIO, 0x55);
	if (sio_read(0x20) != 0x87 || sio_read(0x21) != 0x21) {
		printf("qnapled: no IT8721F Super I/O found, nothing to do\n");
		sio_write(0x02, 0x02);
		return (0);
	}
	sio_write(0x07, LDN_GPIO);
	base = sio_read(0x62) << 8 | sio_read(0x63);
	data = base + 3;

	pin = sio_read(REG_BLINK1_PIN);
	simple = sio_read(REG_SIMPLE_SET4);
	output = sio_read(REG_OUTPUT_SET4);

	/* Only touch GP40 when it is the blinker's pin, or already ours. */
	if (strcmp(cmd, "status") != 0 && base != 0 &&
	    (pin == GP40_LOCATION ||
	    (pin == 0 && (simple & output & GP40_BIT)))) {
		if (strcmp(cmd, "steady") == 0) {
			/* level first, so the pin never outputs "off" */
			outb(data, inb(data) & ~GP40_BIT);
			sio_write(REG_SIMPLE_SET4, simple | GP40_BIT);
			sio_write(REG_OUTPUT_SET4, output | GP40_BIT);
			sio_write(REG_BLINK1_PIN, 0x00);
		} else {
			sio_write(REG_OUTPUT_SET4, output & ~GP40_BIT);
			sio_write(REG_SIMPLE_SET4, simple & ~GP40_BIT);
			sio_write(REG_BLINK1_PIN, GP40_LOCATION);
		}
	} else if (strcmp(cmd, "status") != 0) {
		printf("qnapled: blinker 1 is not on GP40 (F8=%02x), "
		    "leaving the LED alone\n", pin);
		rc = 1;
	}

	printf("blink1-pin=%02x simple-io=%02x output=%02x gp40=%d\n",
	    sio_read(REG_BLINK1_PIN), sio_read(REG_SIMPLE_SET4),
	    sio_read(REG_OUTPUT_SET4), base ? (inb(data) & GP40_BIT) : -1);

	sio_write(0x02, 0x02);	/* leave config mode */
	return (rc);
}
