/*
 * qnapled - drive the QNAP TS-x69 front-panel LEDs from FreeBSD.
 *
 * On the TS-x69 (Granite Well board) the LEDs hang off GPIO pins of the ITE
 * IT8721F Super I/O. The pin assignments come from QNAP's own firmware
 * (/etc/model_3.conf, "TS-669 Pro"); every LED is lit when its pin is low:
 *
 *   power LED            GP40   (SIO:I03:B0)
 *   status LED, green    GP22   (SIO:I01:B2)
 *   status LED, red      GP23   (SIO:I01:B3)   red wins when both are on
 *
 * The BIOS hands the power LED to the chip's "GP LED blinking 1" engine and
 * QNAP's OS later takes it back; plain FreeBSD never does, so it blinks
 * forever. Alternating red/green is made by leaving green on and giving the
 * red pin to blinker 2.
 *
 *   qnapled status                         show the relevant registers
 *   qnapled power steady|blink             power LED on / BIOS blinking
 *   qnapled status-led green|red|alternate|off
 *   qnapled steady | blink                 old names for "power ..."
 *
 * Needs root (/dev/io). Exits without touching anything unless an IT8721F
 * is at 0x2E and the pins are set up as expected (outputs, or handed to a
 * blinker by the BIOS or by us).
 *
 * Registers, all in logical device 7 (GPIO):
 *   F8/F9  GP LED blinking 1 pin mapping / control
 *   FA/FB  GP LED blinking 2 pin mapping / control
 *          pin mapping = (set << 3) | bit; 0x20 = GP40, 0x13 = GP23
 *   C0+n   simple-I/O enable, GPIO set n+1
 *   C8+n   output enable, GPIO set n+1
 *   62/63  simple-I/O base; base + n = data register of set n+1
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
#define REG_BLINK2_PIN	0xfa
#define REG_BLINK2_CTL	0xfb
#define BLINK_1HZ	0x02	/* what the BIOS used for the power LED */

struct pin {
	int	set;		/* GPIO set, 1-based: GP40 -> 4 */
	u_char	bit;		/* mask */
	u_char	location;	/* blinker pin-mapping value */
};

static const struct pin power = { 4, 0x01, 0x20 };	/* GP40 */
static const struct pin green = { 2, 0x04, 0x12 };	/* GP22 */
static const struct pin red   = { 2, 0x08, 0x13 };	/* GP23 */

static u_int base;

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
sio_set(int reg, u_char mask, int on)
{
	u_char v = sio_read(reg);

	sio_write(reg, on ? (v | mask) : (v & ~mask));
}

static int
is_output(const struct pin *p)
{
	return ((sio_read(0xc0 + p->set - 1) & sio_read(0xc8 + p->set - 1) &
	    p->bit) == p->bit);
}

static int
level(const struct pin *p)
{
	return ((inb(base + p->set - 1) & p->bit) != 0);
}

/* Make p a simple-I/O output at the given level, setting the level first so
 * the pin never glitches to the other state. */
static void
drive(const struct pin *p, int high)
{
	u_int port = base + p->set - 1;
	u_char d = inb(port);

	outb(port, high ? (d | p->bit) : (d & ~p->bit));
	sio_set(0xc0 + p->set - 1, p->bit, 1);
	sio_set(0xc8 + p->set - 1, p->bit, 1);
}

/* Hand p to a blinker the way the BIOS does: not simple-I/O, not output. */
static void
release(const struct pin *p)
{
	sio_set(0xc8 + p->set - 1, p->bit, 0);
	sio_set(0xc0 + p->set - 1, p->bit, 0);
}

static int
cmd_power(const char *how)
{
	u_char pin = sio_read(REG_BLINK1_PIN);

	if (!(pin == power.location || (pin == 0 && is_output(&power)))) {
		printf("qnapled: blinker 1 is not on GP40 (F8=%02x), "
		    "leaving the power LED alone\n", pin);
		return (1);
	}
	if (strcmp(how, "steady") == 0) {
		drive(&power, 0);
		sio_write(REG_BLINK1_PIN, 0x00);
	} else {
		release(&power);
		sio_write(REG_BLINK1_PIN, power.location);
	}
	return (0);
}

static int
cmd_status_led(const char *how)
{
	u_char pin2 = sio_read(REG_BLINK2_PIN);

	if (!is_output(&green) ||
	    !(is_output(&red) || pin2 == red.location) ||
	    !(pin2 == 0 || pin2 == red.location)) {
		printf("qnapled: status LED pins are not set up as expected "
		    "(FA=%02x), leaving it alone\n", pin2);
		return (1);
	}
	if (strcmp(how, "alternate") == 0) {
		drive(&green, 0);
		sio_write(REG_BLINK2_CTL, BLINK_1HZ);
		sio_write(REG_BLINK2_PIN, red.location);
		release(&red);
		return (0);
	}
	/* every other state: red back under our control first */
	drive(&red, strcmp(how, "red") != 0);
	sio_write(REG_BLINK2_PIN, 0x00);
	sio_write(REG_BLINK2_CTL, 0x00);
	drive(&green, strcmp(how, "off") == 0);
	return (0);
}

static void
show(void)
{
	printf("power: blink1-pin=%02x gp40=%s %d\n", sio_read(REG_BLINK1_PIN),
	    is_output(&power) ? "out" : "-", level(&power));
	printf("status: blink2-pin=%02x ctl=%02x gp22=%s %d gp23=%s %d\n",
	    sio_read(REG_BLINK2_PIN), sio_read(REG_BLINK2_CTL),
	    is_output(&green) ? "out" : "-", level(&green),
	    is_output(&red) ? "out" : "-", level(&red));
}

static int
usage(void)
{
	fprintf(stderr, "usage: qnapled status\n"
	    "       qnapled power steady|blink\n"
	    "       qnapled status-led green|red|alternate|off\n");
	return (2);
}

int
main(int argc, char **argv)
{
	const char *cmd, *arg;
	int rc;

	if (argc < 2)
		return (usage());
	cmd = argv[1];
	arg = argc > 2 ? argv[2] : "";
	/* old spellings */
	if (argc == 2 && (strcmp(cmd, "steady") == 0 ||
	    strcmp(cmd, "blink") == 0)) {
		arg = cmd;
		cmd = "power";
	}
	if (!((strcmp(cmd, "status") == 0 && argc == 2) ||
	    (strcmp(cmd, "power") == 0 &&
	    (strcmp(arg, "steady") == 0 || strcmp(arg, "blink") == 0)) ||
	    (strcmp(cmd, "status-led") == 0 &&
	    (strcmp(arg, "green") == 0 || strcmp(arg, "red") == 0 ||
	    strcmp(arg, "alternate") == 0 || strcmp(arg, "off") == 0))))
		return (usage());

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
	if (base == 0) {
		printf("qnapled: simple-I/O block is disabled, nothing to do\n");
		sio_write(0x02, 0x02);
		return (0);
	}

	rc = 0;
	if (strcmp(cmd, "power") == 0)
		rc = cmd_power(arg);
	else if (strcmp(cmd, "status-led") == 0)
		rc = cmd_status_led(arg);
	show();

	sio_write(0x02, 0x02);	/* leave config mode */
	return (rc);
}
